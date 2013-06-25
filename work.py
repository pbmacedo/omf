#!/usr/bin/env python

'''
One queue, stores tuples (analysisName, requestType). Or maybe have two queues, whatever.

Adding jobs.
	Put (analysisName, 'toRun') on the queue.
	If that analysis is already on the queue, return failed.
	Update the DB to say queued.
Running jobs.
	Every 1(?) second, all nodes check the queue.
	If there's a toRun there, and we're not full, pop one.
	Start the job.
	Update DB to say queued.
Canceling jobs.
	Every 1(?) second, all nodes check the queue.
	If there's a toKill there, and we're running that analysisName, pop one.
	Kill the PID.

Queue limits?


We need to handle two cases:
1. Filesystem case. Just have a dumb fileQueue class that has no limits, etc.?
2. S3 cluster case. Have a cluterQueue class, and also have a daemon.
'''

import os, tempfile, psutil, time
from multiprocessing import Value, Lock
from threading import Thread, Timer
import studies, analysis, milToGridlab
from boto.sqs.connection import SQSConnection
from boto.sqs.message import Message

JOB_LIMIT = 1

class MultiCounter(object):
	def __init__(self, initval=0):
		self.val = Value('i', initval)
		self.lock = Lock()
	def increment(self):
		with self.lock:
			self.val.value += 1
	def decrement(self):
		with self.lock:
			self.val.value -= 1
	def value(self):
		with self.lock:
			return self.val.value

class LocalWorker:
	def __init__(self):
		self.runningJobCount = MultiCounter(0)
		self.jobRecorder = {}
	def run(self, analysisObject, store):
		runThread = Thread(name=analysisObject.name, target=self.runInBackground, args=[analysisObject, store])
		self.jobRecorder[time.time()] = runThread
		runThread.start()
	def runInBackground(self, anaObject, store):
		# Setup.
		self.runningJobCount.increment()
		def studyInstance(studyName):
			studyData = store.get('Study', anaObject.name + '---' + studyName)
			studyData.update({'name':studyName,'analysisName':anaObject.name})
			moduleRef = getattr(studies, studyData['studyType'])
			classRef = getattr(moduleRef, studyData['studyType'].capitalize())
			return classRef(studyData)
		studyList = [studyInstance(studyName) for studyName in anaObject.studyNames]
		# Run.
		anaObject.run(studyList)
		# Storing result.
		store.put('Analysis', anaObject.name, anaObject.__dict__)
		for study in studyList:
			store.put('Study', study.analysisName + '---' + study.name, study.__dict__)
		self.runningJobCount.decrement()
	def milImport(self, store, feederName, stdString, seqString):
		# Setup.
		self.runningJobCount.increment()
		importThread = Thread(target=self.milImportBackground, args=[store, feederName, stdString, seqString])
		importThread.start()
		self.runningJobCount.decrement()
	def milImportBackground(self, store, feederName, stdString, seqString):
		newFeeder = {'links':[],'hiddenLinks':[],'nodes':[],'hiddenNodes':[],'layoutVars':{'theta':'0.8','gravity':'0.01','friction':'0.9','linkStrength':'5'}}
		[newFeeder['tree'], xScale, yScale] = milToGridlab.convert(stdString, seqString)
		newFeeder['layoutVars']['xScale'] = xScale
		newFeeder['layoutVars']['yScale'] = yScale
		with open('./schedules.glm','r') as schedFile:
			newFeeder['attachments'] = {'schedules.glm':schedFile.read()}
		store.put('Feeder', feederName, newFeeder)
		store.delete('Conversion', feederName)
	def terminate(self, anaName):
		def killingInTheName(anaName):
			try: 
				for runDir in os.listdir('running'):
					if runDir.startswith(anaName + '---'):
						with open('running/' + runDir + '/PID.txt','r') as pidFile:
							os.kill(int(pidFile.read()), 15)
							print 'Terminated', anaName
							return True
			except:
				print 'Missed attempt to terminate', anaName
				return False
		# Try to kill three times.
		for attempt in range(3):
			if killingInTheName(anaName): break
			time.sleep(2)
	def status(self):
		return [[key,self.jobRecorder[key].name,self.jobRecorder[key].is_alive()] for key in self.jobRecorder]

class ClusterWorker:
	def __init__(self, userKey, passKey, workQueueName, terminateQueueName, importQueueName, store):
		self.conn = SQSConnection(userKey, passKey)
		self.workQueue = self.conn.get_queue(workQueueName)
		self.terminateQueue = self.conn.get_queue(terminateQueueName)
		self.importQueue = self.conn.get_queue(importQueueName)
		self.daemonWorker = LocalWorker()
		self.daemonThread = Thread(target=self.__monitorClusterQueue__,args=(passKey, store, self.daemonWorker))
		self.daemonThread.start()
	def run(self, analysisObject, store):
		m = Message()
		m.set_body(analysisObject.name)
		status = self.workQueue.write(m)
		return status
	def terminate(self, anaName):
		m = Message()
		m.set_body(anaName)
		status = self.terminateQueue.write(m)
		return status
	def milImport(self, store, feederName, stdString, seqString):
		store.put('Conversion',feederName,{'stdString':stdString,'seqString':seqString})
		m = Message()
		m.set_body(feederName)
		status = self.importQueue.write(m)
		return status
	def __monitorClusterQueue__(self, passKey, store, daemonWorker):
		print 'Entering Daemon Mode.'
		conn = SQSConnection('AKIAISPAZIA6NBEX5J3A', passKey)
		jobQueue = conn.get_queue('crnOmfJobQueue')
		importQueue = conn.get_queue('crnOmfImportQueue')
		terminateQueue = conn.get_queue('crnOmfTerminateQueue')
		def popJob(queueObject):
			mList = queueObject.get_messages(1)
			if len(mList) == 1:
				anaName = mList[0].get_body()
				queueObject.delete_message(mList[0])
				return anaName
			else:
				return False
		def peakJob(queueObject):
			mList = queueObject.get_messages(1)
			if len(mList) == 1:
				return mList[0]
			else:
				return False
		def endlessLoop():
			if daemonWorker.runningJobCount.value() < JOB_LIMIT:
				anaName = popJob(jobQueue)
				if anaName != False:
					print 'Daemon running', anaName
					thisAnalysis = analysis.Analysis(store.get('Analysis', anaName))
					daemonWorker.run(thisAnalysis, store)
			if daemonWorker.runningJobCount.value() < JOB_LIMIT:
				feederName = popJob(importQueue)
				if feederName != False:
					print 'Daemon importing', feederName
					convo = store.get('Conversion', feederName)
					daemonWorker.milImport(store, feederName, convo['stdString'], convo['seqString'])
			if daemonWorker.runningJobCount.value() > 0:
				termMessage = peakJob(terminateQueue)
				if termMessage != False:				
					runningAnas = [stat[1] for stat in daemonWorker.status() if stat[2]==True]
					anaName = termMessage.get_body()
					if anaName in runningAnas:
						print 'Daemon attempting to terminate', anaName
						daemonWorker.terminate(anaName)
						terminateQueue.delete_message(termMessage)
			# Check again in 1 second:
			Timer(1, endlessLoop).start()
		endlessLoop()
	def status(self):
		return self.daemonWorker.status()
