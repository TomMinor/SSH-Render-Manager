#!/usr/bin/python

try:
  from pexpect import pxssh, EOF, TIMEOUT
except:
  print "Necessary module pexpect not installed"
  sys.exit(1)

import sys
import time
import os
import getpass
from select import select, poll
import subprocess
import re
import logging
import json
import uuid

"""
PROBLEMS:
 - Dodgy things happen with shared SSH connections enabled (pausing this ssh session pauses them all for example, caused a hideous system freeze up when I locked my machine and tried to log back in while I left the Job paused).
    + Disable shared SSH connections when using this.

 - Pausing/unpausing (SIGSTOP/SIGCONT respectively) fails to affect the render process, unsure if it pauses the shell itself successfully. This worked at one point, unsure what broke it.
    + This functionality is disabled in the UI for now.

 - Primitive way of error checking, will first check the session output for COMPLETE_SUCCESS or COMPLETE_ERROR (these are printed when the render process ends) and then grabs the latest output from the log files, using it to parse out the progress percentage & current frame number.
    + The job output is updated as the session is polled, this is displayed to the user via the UI and may be used for error checking if the script fails to figure it out itself.

 - If the process was killed remotely (ie, ssh'ing directly into the machine and killing the maya.bin process) then the job may not notice and will just carry on running without ever changing state. This isn't a huge problem as you will probably notice that it has timed out and can terminate it manually. 
    + Adding a timeout for how long the job will go without an obvious update could work 



WHAT IT DOES:
 - Start a maya render process on a remote host
 - Parse the output to figure out the render status
 - On process exit > 
  - If COMPLETE_SUCCESS is read then the session is terminated
  - If COMPLETE_ERROR is read then the session is terminated and the log file parsed for 'maya exited with error(x)', then we can grab the error code

 - 
"""

class Job:
  """
  Perhaps this should be broken up, it's pretty huge now

  TODO:
    Does setting the frame range update the output? s-100 -> scene_0100.tga
  """

  STATE = {
      'i' : "Idle",
      'r' : "Running",
      'p' : "Paused",
      'e' : "Error",
      'c' : "Finished"
      }

  ERROR = {
      0 : 'Success'
      }

  def __init__(self, 
                host, 
                scenePath, 
                frameRange=None, 
                outputPath=None, 
                camOverride=None, 
                resolutionOverride=None, 
                user=None, 
                binPath='/opt/autodesk/maya2014-x64/bin/Render', 
                logPath=None):

    # Store the original args to restarting the job
    self.originalArgs = locals()
    self.originalArgs.pop('self')

    self.logger = logging.getLogger(__name__)
    if logPath != None:
      logName = os.path.join('%s@%s_%s.log' % (os.path.basename(scenePath), host, uuid.uuid4()))
      logPath = os.path.expanduser(logPath)
      if not os.path.exists(logPath):
        os.makedirs(logPath)

      self._jobLogFile = os.path.join(logPath, logName)
      try:
        with open(self._jobLogFile, 'w') as f:
          f.write('')
      except IOError, e:
        raise
        
    handler = logging.FileHandler(self._jobLogFile)
    self.logger.addHandler(handler)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    handler.setFormatter(formatter)
    self.logger.setLevel(logging.DEBUG)

    # The regex pattern to search for in the output to retrieve the current percentage 
    self.progressRe = re.compile('JOB[\s]*[\d]+\.[\d]+[\s]*[\d]+ MB progr:[\s]*[\d]+\.[\d]+\%[\s]*rendered on .*')
    self._progress = 0

    # Seek value for reading from file
    self.p = 0

    self._host = host
    self._binPath = binPath
    self._scenePath = scenePath
    self._outputPath = outputPath
    self._frameRange = ( int(frameRange[0]), int(frameRange[1]) )
    self._currentFrame = 0
    self._camOverride = camOverride
    self._resOverride = resolutionOverride

    self._maxFrame = self._frameRange[1] - self._frameRange[0]

    if self._maxFrame < 0:
      self.logger.error('Negative frame range entered')
      raise ValueError('Negative frame range')

    logDir = os.path.expanduser('~/.rendermanager/renderLogs/%s/%s/' % (os.path.splitext(os.path.basename(self._scenePath))[0], self._host))
    logFile = '%s_%s.log' % (time.strftime("D%d:%m:%Y"), time.strftime("T%H:%M:%S")) 

    if not os.path.exists(logDir):
      os.makedirs(logDir)

    self._logPath = os.path.join(logDir, logFile)

    try:
      with open(self._logPath, 'w') as f:
        f.write('')
    except IOError:
      raise

    self.logger.debug('Maya job log path: %s' % self._logPath)

    self._user = user if user else getpass.getuser() 
    self.logger.debug('Session user : %s' % self._user)

    self.logger.debug('Parsing scene file for output prefix...')
    try:
      with open(os.path.expanduser(self.scenePath)) as f:
          for line in f:
            if 'setAttr ".ifp" -type "string"' in line:
              self.outputPrefix = line.split('"')[-2]
              self.logger.debug('File name prefix found, using %s' % self.outputPrefix)
              break
          else:
            self.outputPrefix = os.path.splitext(os.path.basename(self.scenePath))[0]
            self.logger.debug('File name prefix not set, using scene name (%s)' % self.outputPrefix)
    except IOError:
      raise

    self._processArgs = []

    if frameRange:
      self._processArgs.append( ('s', str(self._frameRange[0])) ) 
      self._processArgs.append( ('e', str(self._frameRange[1])) )

    if resolutionOverride:
      self._processArgs.append( ('x', str(resolutionOverride[0])) )
      self._processArgs.append( ('y', str(resolutionOverride[0])) )
      
    if camOverride:
      self._processArgs.append( ('cam', camOverride) )

    # Verbose output
    self._processArgs.append( ('v', '5') )
    self._processArgs.append( ('verb', '') )
    self._processArgs.append( ('rep', '') )
    
    # Force renderer to mental ray
    self._processArgs.append( ('r', 'mr') )  
    # MR specific args
    self._processArgs.append( ('art', '') ) 
    self._processArgs.append( ('aml', '') ) 
    self._processArgs.append( ('at', '') ) 
      
    self._processArgs.append( ('log', self._logPath) )

    #self.logger.debug('Process args : \n%s' % json.dumps(self._processArgs, indent=3))
    
    # Construct process call
    self._processCall = [self._binPath.replace(' ', '\\ ')]
    for arg in self._processArgs:
        self._processCall.append( '-%s %s' % arg)

    self._processCall.append('\"%s\"' % self._scenePath)

    self._processCall = " ".join(self._processCall)

    self.logger.debug('Process call : %s' % self._processCall)
    
    self._output = []
    self._sshOutput = '' 
    self.process = pxssh.pxssh()
    
    self.__setState('i')
    self._errorCode = None

    try:
        self.process.login(self._host, self._user)
    except pxssh.ExceptionPxssh as e:
        self.logger.error('Cannot log on as %s@%s' % (self._user, self._host))
        raise 
    
  def __str__(self):
    return '[%s] : %s@%s : { Frame %d/%d } %.2f%%' % (self.state, os.path.basename(self._scenePath), self.host, self._currentFrame, self.totalFrames, self.progress)

  def __setState(self, state):
    if hasattr(self, '_state'):
      self.logger.debug("%s -> %s" % (Job.STATE[self._state], Job.STATE[state]))
    else:
      self.logger.debug("Setting initial state to %s" % Job.STATE[state])
    self._state = state

  def __setProgress(self, value):
    self.logger.info('Frame progress : %.2f' % value)
    self._progress = value

  def __onComplete(self, success):
    self.logger.info('Job finished')
    if success:
      self.logger.info('Success')
      self.__setState('c')
      self.__setProgress(0.0)
      self._currentFrame = self._maxFrame

      self._output = []
      try:
        with open(self._logPath) as logFile:
          for line in logFile:
            self._output.append(line.strip())
      except IOError, e:
        self.logger.error(e)
    else:
      self.__setState('e')
      if self._errorCode != None:
        try:
          with open(self._logPath) as logFile:
            self.parseErrorcode([line for line in logFile])
        except IOError, e:
          self.logger.error(e)

    self.close()

  def parseErrorcode(self, lines):
      for line in lines:
          if 'Maya exited with status' in line:
              self._errorCode = int(re.findall('\d+', line)[0])
              break
      else:
        self._errorCode = None 

      if self._errorCode == 0:
        self.__setState('c')
      elif self._errorCode > 0:
        self.logger.error('Error : (%d)' % int(self._errorCode))
      else:
        self.logger.error('Unknown error')

  def run(self):
    if self._state == 'i':
      self.logger.info('Executing remote process on %s' % self.host)
      # Run the process and evaluate it's return value when complete, ensuring we capture success/failure
      self.process.sendline(';'.join([r'nice %s' % self._processCall,
                                      r"RETVAL=$?",
                                      r"[ $RETVAL -eq 0 ] && echo COMPLETE_SUCCESS",
                                      r"[ $RETVAL -ne 0 ] && echo COMPLETE_ERROR"]
                                      ))

      self.logger.info('Ignoring initial output')

      # Read until the job starts, and write any errors to file if it cannot
      tmp = ''
      while tmp.split('\n')[-1] != 'Locale is: "Locale:en_GB.utf8 CodeSet:UTF-8"':
        try:
          tmp += self.process.read_nonblocking()
          lines = tmp.split('\n')
          if 'COMPLETE_ERROR' == lines[-1]:
            # Premature exit
            try:
              self.logger.error(lines[-2])
            except Exception:
              pass

            try:
              with open(self._logPath, 'a+') as mayaLog:
                mayaLog.write(tmp)
            except IOError, e:
              self.logger.error(e)

            self.logger.error('Prematurely exited process')
            self.__onComplete(success=False)
            break
        except TIMEOUT, e:
          pass
        except ValueError, e:
          self.logger.error(e)
          self.__onComplete(success=False)
          break
    
      self.__setState('r')

  def update(self):
    if not self.process.isalive():
        self.__onComplete(success=False)

    if not self.completed():
      try:
        self._sshOutput += str(self.process.read_nonblocking()) 
      except TIMEOUT, e:
        self.logger.debug(e)
      except ValueError, e:
        self.logger.error(e)
        self.__setState('e')
        return
      except EOF, e:
        self.logger.error(e)
        self.__setState('e')
        return
      
      sshOutputNew = self._sshOutput.split('\n')
      for line in sshOutputNew:
        if 'COMPLETE_SUCCESS' in line:
          self.__onComplete(success=True)

        if 'COMPLETE_FAILURE' in line: 
          self.__onComplete(success=False)

      try:
        with open(self._logPath, 'r') as f:
            f.seek(self.p)
            latest_data = f.read().split('\n')
            #self.p = f.tell()
            self.p = 0 # nasty performance, but for some reason it doesn't like reading the data properly if we read from the last position

            self.output = [ line for line in latest_data ]

            progressResults = self.progressRe.findall('\n'.join(latest_data), re.MULTILINE)
            progressResult_LineNum = -1
            renderingStats_LineNum = -1
            if progressResults:
                for num, line in enumerate(latest_data):
                    if line == progressResults[-1]:
                        progressResult_LineNum = num
                    if 'rendering statistics' in line:
                        renderingStats_LineNum = num
                        if self._currentFrame != self._maxFrame:
                          self.logger.debug('Incrementing frame counter')
                          self._currentFrame += 1
                    if 'Maya exited with status' in line:
                        self._errorCode = int(re.findall('\d+', line)[0])
                        if self._errorCode != 0:
                          self.logger.error('Error : (%d)' % int(self._errorCode))
                          self.__onComplete(success=False)
                        else:
                          self.__onComplete(success=True)

                if progressResult_LineNum > renderingStats_LineNum:
                    self.logger.debug('Getting remaining frame progress')
                    percentage = re.findall(r'\d+.\d+%', progressResults[-1])[0]
                    self.__setProgress(float(percentage[:-1]))
                else:
                    self.__setProgress(100.0)
      except IOError, e:
        self.logger.error(e.message)

  def pause(self):
    if not self._state == 'p':
        self.logger.info('Job paused')
        self.process.kill(23) #SIGSTOP
        self.__setState('p')

  def resume(self):
    if self._state == 'p':
        self.logger.info('Job resumed')
        self.process.kill(25) #SIGCONT
        self.__setState('r')

  def kill(self):
    self.resume()
    if self._state == 'r':
        self.logger.info("Killing %s" % self._binPath)
        self.process.kill(9) #SIGKILL
        self._state = 'e' if self.errorCode else 'c'
    else:
        self._state = 'e' if self.errorCode else 'c'

  def close(self):
    self.logger.info('Closing session')
    self.kill()

    if not self.completed(): 
        self.__onComplete(success=False)

    try:
        self.process.logout()
    except OSError as e:
        self.logger.error(e.message)
    except ValueError as e:
        self.logger.error(e.message)

    try:
        with open(self._logPath, 'r') as mayaLog:
            self._output = [ line for line in mayaLog ]
    except IOError as e:
        self.logger.error(e.message)

    self.process.close(force=True)

  def getNewInstanceofJob(self):
    return Job(**self.originalArgs)

  def completed(self):
    return (self._state == 'c' or self._state == 'e')

  @property
  def host(self):
    return self._host

  @property
  def binaryPath(self):
    return self._binPath

  @property
  def scenePath(self):
    return self._scenePath

  @property
  def outputPath(self):
    return self._outputPath

  @property
  def frameRange(self):
    return self._frameRange

  @property
  def cameraOverride(self):
    if self._camOverride:
      return self._camOverride
    else:
      return 'n/a'

  @property
  def resolutionOverride(self):
    if self._resOverride:
      return self._resOverride
    else:
      return ('n/a', 'n/a')

  @property
  def logPath(self):
    return self._logPath

  @property
  def jobLogPath(self):
    return self._jobLogFile

  @property
  def sessionUser(self):
    return self._user

  @property
  def state(self):
    return Job.STATE[str(self._state)]

  @property
  def outputPrefix(self):
    return self.outputPrefix

  @property
  def output(self):
    return self._output

  @property
  def frameProgress(self):
    return self._progress

  @property
  def progress(self):
    return ((100.0*self._currentFrame) + self._progress) / float(self._maxFrame+1)

  @property
  def currentFrame(self):
    return self._currentFrame

  @property
  def errorCode(self):
    if hasattr(self, '_errorCode'):
      return self._errorCode
    else:
      return 0

  @property
  def errorCodeDetail(self):
    if self.errorCode in Job.ERROR:
      return Job.ERROR[self.errorCode]
    else:
      return 'Unknown error'

  @property
  def totalFrames(self):
    return self._maxFrame

  @property
  def paused(self):
    return self._state == 'p'

  @property
  def running(self):
    return self._state == 'r'
