#!/usr/bin/python

import Tkinter as tk
import ttk
import tkMessageBox as tkmsg
import tkFileDialog as tkfile

import sys
import re
import os.path
import shlex
import time
import getpass
import subprocess
import signal
import logging
import json

from threading import Timer

#import mayaJob
import dummyJob as mayaJob

"""
Not the most readable code in places, but it works for what it is.

PROBLEMS (just in the GUI component, mayaJob has it's own problems) : 
  - Windows paths ('D:\images') won't be read from the workspace file properly, should add in a check for this but not a huge problem yet
  - Lowering update rate when the screensaver is active only works on Gnome (Red Hat uses Gnome so not a problem at uni)
  - SSH keys _must_ be setup for all the hosts for this to work properly.
"""

def secureCopy(host, src, dst, logger=None, limit=8912):
    # Escape spaces
    src = src.replace(' ', r'\ ')
    dst = dst.replace(' ', r'\ ')

    command = 'scp -C -B -l %d %s:\"%s\" \"%s\"' % (limit, host, src, dst)
    try:
        scp = subprocess.Popen(command, shell=True).wait()
    except OSError, e:
        if logger:
            logger.error('Command error : %s ' % command)
            logger.error(e)
        else:
            print command
            print e

def displayError(type_, msg, logger=None):
    output = 'Error %s : %s' % (type_, msg)
    if logger:
        logger.error(output, exc_info=sys.exc_info())
    else:
        print output

    tkmsg.showinfo(type_, msg)

def verifyHost(host, timeout=1):
    """
    Will fail if the host is not accessible 
    """
    print "Verifying host %s" % host
    result = subprocess.Popen(['ssh', '-o', 'ConnectTimeout=%d' % timeout, host, 'hostname'], 
                                stdout=subprocess.PIPE, 
                                stderr=subprocess.PIPE).wait()
    # Returns 255 if the host is not accessible
    # Returns 130 if the wrong password was entered (no point checking this right now)
    return result != 255

def modifyDisabledText(entryText, msg, startCursor = 0, colour='#000000', multiLine=False):
    entryText.config(state='normal')
    entryText.delete(startCursor, tk.END)
    entryText.insert(tk.END, msg)

    if multiLine:
        entryText.see(tk.END)
    else:
        entryText.icursor(tk.END)

    entryText.config(state='disabled')
    entryText.config(fg=colour)

def screensaverEnabled():
    query = subprocess.Popen('gnome-screensaver-command -q | grep "is inactive"', 
                            shell=True, 
                            stdout=subprocess.PIPE, 
                            stderr=subprocess.PIPE)
    return query.wait()

class ManagerUI(tk.Frame):
    # When the screensaver is detected the update rate for the job update thread
    # is considerably reduced to SCREENSAVER_ON_DELAY, these is no need to have 
    # real time updating if no one is physically at the manager.
    # Maybe only checking for updates every half an hour will be nicer on the 
    # network too. As soon as the screen gets unlocked, the update rate becomes
    # SCREENSAVER_OFF_DELAY
    SCREENSAVER_ON_DELAY = 1800.0
    SCREENSAVER_OFF_DELAY = 0.1
    UI_REFRESH_DELAY = 100

    MIN_WINDOW_SIZE = (800, 600)
    APPDIR = os.path.expanduser('~/.rendermanager')

    updateThreadDelay = SCREENSAVER_OFF_DELAY
    
    def __init__(self, parent):
        tk.Frame.__init__(self, parent)

        logging.basicConfig(level=logging.DEBUG)
        self.logger = logging.getLogger(__name__)

        if not os.path.exists(ManagerUI.APPDIR):
            os.makedirs(ManagerUI.APPDIR)

        mainLogPath = os.path.join(ManagerUI.APPDIR, 'main.log')

        with open(mainLogPath, 'w') as f:
          f.write('')
          
        handler = logging.FileHandler(mainLogPath)
        formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

        self.logger.info('======= Starting render manager =======')

        self.workspacePath = tkfile.askopenfilename( **{ 
          'filetypes':[('Maya workspace files', '.mel')],
          'initialdir':os.path.expanduser('~'),
          'title':'Select project workspace file' })

        self.logger.info('Loading workspace from user directory %s' % self.workspacePath)

        outputDirectoryPat = re.compile(r'workspace -fr \"images\"')

        self.logger.info('Workspace file exists')
        self.logger.info('Reading file...')

        try:
            with open(self.workspacePath) as f:
                self.logger.info("Checking for \"images\" entry...")

                for num, line in enumerate(f):
                    if outputDirectoryPat.match(line):
                        self.logger.info("Success on line %d : %s" % (num, line))

                        outputDir = shlex.split(line)[3].replace('\"', '')[:-1]

                        self.logger.info("Using output directory %s" % outputDir)
                        break
                else:
                    raise ValueError
        except IOError:
            displayError('Error', "Invalid workspace file '%s', exiting" % self.workspacePath, self.logger)
            sys.exit(1)
        except ValueError:
            displayError('Invalid workspace file', 'Failed to find render path  in workspace file, exiting', self.logger)
            sys.exit(1)

        # Check if the output dir is relative to the workspace
        if not os.path.isabs(outputDir):
            outputDir = os.path.join(os.path.dirname(self.workspacePath), outputDir)
            self.logger.info("Relative path detected, expanding to %s" % outputDir)

        hostsDir = os.path.join(ManagerUI.APPDIR, 'hostMachines')
        self.logger.info("Loading hosts from %s" % hostsDir)

        self.hosts = []
        
        try:
            with open(hostsDir) as f:
                for line in f:
                    if line:
                        # Ignore commented lines
                        if line[0] != '#':
                            hostMachine = line.rstrip()
                  
                            if verifyHost(hostMachine):
                                self.hosts.append(hostMachine)
                            else:
                                self.logger.info("Inaccessible host %s, skipping" % hostMachine)
                else:
                    if not self.hosts:
                        raise IOError("Hosts file '%s' contains no hosts" % hostsDir)
        except IOError, e:
            displayError('File not found', e, self.logger)

        # Remove duplicates
        self.hosts = list(set(self.hosts))

        self.renderJobs = []
        self.renderQueue = {}
        self.selectedJobID = -1
        self.lastOutput = []
        
        self.defaults = {}
        self.defaults['binDir'] = '/opt/autodesk/maya2014-x64/bin/Render'
        self.defaults['outputDir'] = outputDir
        self.defaults['camOverride'] = 'persp'
        self.defaults['resolutionOverride'] = (640, 480)
        self.defaults['frames'] = (0, 0)

        #self.logger.debug('Defaults \n %s' % json.dumps(self.defaults, indent=3))

        self.fileOpt = {}
        self.fileOpt['defaultextension'] = ''
        self.fileOpt['filetypes'] = [('Maya scene files', '.ma .mb')]
        self.fileOpt['title'] = 'File Browser'
        self.fileOpt['initialdir'] = os.path.expanduser('~')

        #self.logger.debug('File options : \n %s' % json.dumps(self.fileOpt, indent=3))

        self.dirOpt = {}
        self.dirOpt['mustexist'] = True
        self.dirOpt['title'] = 'Directory Browser'

        #self.logger.debug('Directory options : \n %s' % json.dumps(self.dirOpt, indent=3))
         
        self.parent = parent
        self.parent.protocol('WM_DELETE_WINDOW', self.onExit)
        self.parent.minsize(*ManagerUI.MIN_WINDOW_SIZE)

        signal.signal(signal.SIGINT, self.onKill) 
        self.parent.after(ManagerUI.UI_REFRESH_DELAY, self.refreshUI)
        self.updateThread = Timer(ManagerUI.updateThreadDelay, self.update).start()
        self.shouldExit = False

        self.initWidgets()
        
    def refreshUI(self):
        if screensaverEnabled:
            if ManagerUI.updateThreadDelay != ManagerUI.SCREENSAVER_OFF_DELAY:
                self.logger.debug('User is active, decreasing update thread delay to %f' % ManagerUI.SCREENSAVER_OFF_DELAY)
                ManagerUI.updateThreadDelay = ManagerUI.SCREENSAVER_OFF_DELAY
                if self.updateThread: 
                    self.updateThread.cancel()
                self.updateThread = Timer(ManagerUI.updateThreadDelay, self.update).start()
        else:
            if ManagerUI.updateThreadDelay != ManagerUI.SCREENSAVER_ON_DELAY:
                self.logger.debug('User is inactive, increasing update thread delay to %f' % ManagerUI.SCREENSAVER_ON_DELAY)
                ManagerUI.updateThreadDelay = ManagerUI.SCREENSAVER_ON_DELAY
                if self.updateThread: 
                    self.updateThread.cancel()
                self.updateThread = Timer(ManagerUI.updateThreadDelay, self.update).start()

        selection = self.jobListbox_list.curselection()

        # Refresh list
        self.jobListbox_list.delete(0, tk.END)
        for job in self.renderJobs:
            self.jobListbox_list.insert(tk.END, str(job))

            if job.state == 'Finished':   self.jobListbox_list.itemconfig(tk.END, bg='green')
            elif job.state == 'Running':  self.jobListbox_list.itemconfig(tk.END, bg='orange')
            elif job.state == 'Error':    self.jobListbox_list.itemconfig(tk.END, bg='red')
            elif job.state == 'Idle':     pass

        for select in selection:
            self.jobListbox_list.select_set(select)

        if self.selectedJobID != -1:
            job = self.renderJobs[self.selectedJobID]

            if self.lastOutput != job.output:
                logOutput = ''.join([ '%s\n' % line for line in job.output ])
                modifyDisabledText(self.jobOut, logOutput, colour='#FFFFFF', multiLine=True, startCursor='1.0')
                self.lastOutput = job.output

                modifyDisabledText(self.entCurrentFrame, job.currentFrame)
                self.prgRenderProgressFrame["value"] = job.frameProgress
                self.prgRenderProgress["value"] = job.progress

            self.btnJobRestart.config(state=tk.NORMAL)
            self.btnJobRemove.config(state=tk.NORMAL)
            self.btnJobKill.config(state=tk.NORMAL)
        else:
            self.btnJobRestart.config(state=tk.DISABLED)
            self.btnJobRemove.config(state=tk.DISABLED)
            self.btnJobKill.config(state=tk.DISABLED)

        self.parent.after(ManagerUI.UI_REFRESH_DELAY, self.refreshUI)

    def update(self):
        if not self.shouldExit:
            for host in self.renderQueue:
                if self.renderQueue[host]:
                    topJob = self.renderQueue[host][0]
                    if not topJob.running:
                        try:
                            self.logger.info('Starting next job on host %s' % topJob.host)
                            topJob.run()
                        except IOError:
                            topJob.close()
                            displayError('Update job', 'Cannot start job on %s' % topJob.host, self.logger)
                            self.renderQueue[host].pop()
                    elif topJob.completed():
                        self.logger.info('Popping finished job off queue on %s' % host)
                        self.renderQueue[host].pop()
            
            for job in self.renderJobs:
                if not job.completed():
                    job.update()
                        
            self.updateThread = Timer(ManagerUI.updateThreadDelay, self.update).start()
        else:
            self.logger.info('Update thread closing...')
            for i, job in enumerate(self.renderJobs):
                self.logger.info('Closing job #%d...' % i)
                job.close()
                self.logger.info('Done')
            self.logger.info('All jobs closed, terminating thread')
            del self.renderJobs

    def initWidgets(self):
        self.logger.info('Building interface...')

        self.parent.title("Render Manager")        
        self.style = ttk.Style()
        self.style.theme_use("default")

        # Layout #

        self.parent.columnconfigure(0, weight=1)
        self.parent.rowconfigure(0, weight=1)

        self.mainPane = tk.PanedWindow(self.parent, width=800, height=600, orient=tk.VERTICAL, relief=tk.GROOVE)
                
        self.mainPane.pack(fill=tk.BOTH, expand=1)

        self.topFrame = tk.PanedWindow(self.mainPane, width=800, height=300)
        self.topFrame.grid(row=0, column=0, columnspan=2, rowspan=2, sticky=tk.N+tk.S+tk.W+tk.E)
        self.topFrame.columnconfigure(0, weight=1)
        self.topFrame.columnconfigure(1, weight=1)
        self.topFrame.rowconfigure(0, weight=1)
        self.topFrame.rowconfigure(1, weight=1)
        self.mainPane.add(self.topFrame)

        self.jobList = tk.Frame(self.topFrame, borderwidth=5, width=500, relief=tk.GROOVE)
        self.jobList.grid(row=0, column=0)
        self.jobList.columnconfigure(0, weight=1)
        self.jobList.rowconfigure(0, weight=1)
        self.topFrame.add(self.jobList)
        
        self.jobInfo = tk.Frame(self.topFrame, width=400)
        self.jobInfo.grid(row=0, column=1)
        self.jobInfo.columnconfigure(1, weight=1)
        self.jobInfo.rowconfigure(0, weight=1)
        self.topFrame.add(self.jobInfo)
        
        self.jobOutput = tk.Frame(self.mainPane, width=800, height=300, relief=tk.GROOVE)
        self.jobOutput.grid(row=0, column=0, columnspan=2, rowspan=1, sticky=tk.N+tk.S+tk.W+tk.E)
        self.jobOutput.columnconfigure(0, weight=1)
        self.jobOutput.rowconfigure(0, weight=1)
        self.mainPane.add(self.jobOutput)

        # Menu #
        self.menubar = tk.Menu(self.parent)
        self.parent.config(menu=self.menubar)
        
        self.fileMenu = tk.Menu(self.menubar)
        self.fileMenu.add_command(label="Copy files", command=self.copyJobFiles)
        self.fileMenu.add_command(label="Exit", command=self.onExit)
        self.menubar.add_cascade(label="File", menu=self.fileMenu)

        # Job Listbox # 

        self.jobListbox = tk.Frame(self.jobList)
        self.jobListbox_scr = tk.Scrollbar(self.jobListbox)
        self.jobListbox_list = tk.Listbox(self.jobListbox, yscrollcommand=self.jobListbox_scr.set)
        self.jobListbox_scr.config(command=self.jobListbox_list.yview)

        self.jobListbox_list.bind("<<ListboxSelect>>", self.onJobSelect)
        self.jobListbox_list.bind("<Double-Button-1>", lambda x: self.copyJobFiles() )
        self.jobListbox_list.grid(row=0, column=0, sticky="nwes")
        self.jobListbox_list.rowconfigure(0, weight=1)
        self.jobListbox_list.columnconfigure(0, weight=1)

        self.jobListbox_scr.grid(row=0, column=1, stick="news")
        self.jobListbox_scr.rowconfigure(0, weight=1)

        self.jobListbox.grid(row=0, columnspan=6, sticky="nsew")
        self.jobListbox.rowconfigure(0, weight=1)
        self.jobListbox.columnconfigure(0, weight=1)

        # Job list buttons#

        btnPad = 3

        columnCounter = 1

        self.btnJobAdd = ttk.Button(self.jobList, text="New", command=self.onJobAdd)
        self.btnJobAdd.grid(row=1, column=columnCounter, sticky="n", padx=btnPad, pady=btnPad)
        self.btnJobAdd.rowconfigure(0, weight=0)
        self.btnJobAdd.columnconfigure(0, weight=0)

        columnCounter += 1

        self.btnJobRestart = ttk.Button(self.jobList, text="Restart", command=self.onJobRestart)
        self.btnJobRestart.grid(row=1, column=columnCounter, sticky="n", padx=btnPad, pady=btnPad)
        self.btnJobRestart.rowconfigure(0, weight=0)
        self.btnJobRestart.columnconfigure(0, weight=0)

        columnCounter += 1

        self.btnJobRemove = ttk.Button(self.jobList, text="Remove", command=self.onJobRemove)
        self.btnJobRemove.grid(row=1, column=columnCounter, sticky="n", padx=btnPad, pady=btnPad)
        self.btnJobRemove.rowconfigure(0, weight=0)
        self.btnJobRemove.columnconfigure(0, weight=0)

        columnCounter += 1

        self.btnJobPause = ttk.Button(self.jobList, text="Pause", command=self.onJobPauseToggle, state=tk.DISABLED)
        self.btnJobPause.grid(row=1, column=columnCounter, sticky="n", padx=btnPad, pady=btnPad)
        self.btnJobPause.rowconfigure(0, weight=0)
        self.btnJobPause.columnconfigure(0, weight=0)

        columnCounter += 1

        self.btnJobKill = ttk.Button(self.jobList, text="Kill", command=self.onJobKill)
        self.btnJobKill.grid(row=1, column=columnCounter, sticky="n", padx=btnPad, pady=btnPad)
        self.btnJobKill.rowconfigure(0, weight=0)
        self.btnJobKill.columnconfigure(0, weight=0)

        # Job Info #

        btnPad = 3

        rowCounter = 1

        # Host
        self.lblHost = tk.Label(self.jobInfo, text="Host Machine : ")
        self.lblHost.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)
        self.jobInfo.rowconfigure(0, weight=1)
        self.entHost = tk.Entry(self.jobInfo, width=20, state=tk.DISABLED)
        self.entHost.grid(row=rowCounter, column=1, sticky='NW')

        rowCounter += 1

        # Exe path
        self.lblBinPath = tk.Label(self.jobInfo, text="Binary Path : ")
        self.lblBinPath.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)
        self.entBinPath = tk.Entry(self.jobInfo, width=20, state=tk.DISABLED)
        self.entBinPath.grid(row=rowCounter, column=1, sticky='NW')

        rowCounter += 1

        # Scene path
        self.lblScenePath = tk.Label(self.jobInfo, text="Scene Path : ")
        self.lblScenePath.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)
        self.entScenePath = tk.Entry(self.jobInfo, width=20, state=tk.DISABLED)
        self.entScenePath.grid(row=rowCounter, column=1, sticky='NW')

        rowCounter += 1

        # Frame range
        self.lblFrameRange = tk.Label(self.jobInfo, text="Frame range : ")
        self.lblFrameRange.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)
        self.frmFrameRange = tk.Frame(self.jobInfo)
        self.frmFrameRange.grid(row=rowCounter, column=1, sticky='NW')

        self.lblFrameRange_1 = tk.Label(self.frmFrameRange, text="Start:")
        self.lblFrameRange_1.grid(row=0, column=0, sticky='NE')
        self.entFrameRange_1 = tk.Entry(self.frmFrameRange, width=5, state=tk.DISABLED)
        self.entFrameRange_1.grid(row=0, column=1, sticky='NW')

        self.lblFrameRange_2 = tk.Label(self.frmFrameRange, text="End:")
        self.lblFrameRange_2.grid(row=0, column=2, sticky='NE')
        self.entFrameRange_2 = tk.Entry(self.frmFrameRange, width=5, state=tk.DISABLED)
        self.entFrameRange_2.grid(row=0, column=3, sticky='NW')

        rowCounter += 1

        # Resolution override
        self.lblResOverride = tk.Label(self.jobInfo, text="Resolution override : ")
        self.lblResOverride.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)
        self.frmResOverride = tk.Frame(self.jobInfo)
        self.frmResOverride.grid(row=rowCounter, column=1, sticky='NW')

        self.lblResOverride_x = tk.Label(self.frmResOverride, text="Width:")
        self.lblResOverride_x.grid(row=0, column=0, sticky='NE')
        self.entResOverride_x = tk.Entry(self.frmResOverride, width=5, state=tk.DISABLED)
        self.entResOverride_x.grid(row=0, column=1, sticky='NW')

        self.lblResOverride_y = tk.Label(self.frmResOverride, text="Height:")
        self.lblResOverride_y.grid(row=0, column=2, sticky='NE')
        self.entResOverride_y = tk.Entry(self.frmResOverride, width=5, state=tk.DISABLED)
        self.entResOverride_y.grid(row=0, column=3, sticky='NW')

        rowCounter += 1

        # Camera override 
        self.lblCameraOverride = tk.Label(self.jobInfo, text="Camera override : ")
        self.lblCameraOverride.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)
        self.entCameraOverride = tk.Entry(self.jobInfo, width=20, state=tk.DISABLED)
        self.entCameraOverride.grid(row=rowCounter, column=1, sticky='NW')

        rowCounter += 1

        # Output path
        self.lblOutputPath = tk.Label(self.jobInfo, text="Output Path (On remote host) : ")
        self.lblOutputPath.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)
        self.entOutputPath = tk.Entry(self.jobInfo, width=20, state=tk.DISABLED)
        self.entOutputPath.grid(row=rowCounter, column=1, sticky='NW')

        rowCounter += 1

        # Progress 
        self.lblCurrentFrame = tk.Label(self.jobInfo, text="Current Frame : ")
        self.lblCurrentFrame.grid(row=rowCounter, column=0, sticky='N', padx=btnPad, pady=btnPad, columnspan=1)
        self.entCurrentFrame = tk.Entry(self.jobInfo, width=20, state=tk.DISABLED)
        self.entCurrentFrame.grid(row=rowCounter, column=1, sticky='NW')

        rowCounter += 1

        self.lblRenderProgress = tk.Label(self.jobInfo, text="Frame Progress : ")
        self.lblRenderProgress.grid(row=rowCounter, column=0, sticky='N', padx=btnPad, pady=btnPad, columnspan=1)

        self.prgRenderProgressFrame = ttk.Progressbar(self.jobInfo, orient="horizontal", length=200, mode="determinate")
        self.prgRenderProgressFrame.grid(row=rowCounter, column=1, sticky='W', columnspan=1)

        rowCounter += 1

        self.prgRenderProgress = ttk.Progressbar(self.jobInfo, orient="horizontal", length=400, mode="determinate")
        self.prgRenderProgress.grid(row=rowCounter, column=0, sticky='W', columnspan=2)

        # Terminal output #
        self.frmJobOut = tk.Frame(self.jobOutput)
        self.frmJobOut.rowconfigure(0, weight=1)
        self.frmJobOut.columnconfigure(0, weight=1)
        self.frmJobOut.grid(row=0, column=0, sticky="news")

        self.jobOut_scr = tk.Scrollbar(self.frmJobOut)

        self.jobOut = tk.Text(self.frmJobOut, fg="white", bg="black", state=tk.DISABLED, yscrollcommand=self.jobOut_scr.set)
        self.jobOut.rowconfigure(0, weight=1)
        self.jobOut.columnconfigure(0, weight=1)
        self.jobOut.grid(row=0, column=0, sticky="news")

        self.jobOut_scr.config(command=self.jobOut.yview)
        self.jobOut_scr.rowconfigure(0, weight=1)
        self.jobOut_scr.grid(row=0, column=1, sticky="news")
  
        # Terminal input #
        self.jobOutEntry = tk.Entry(self.jobOutput, fg="white", bg="#111111")
        self.jobOutEntry.rowconfigure(1, weight=1)
        self.jobOutEntry.columnconfigure(0, weight=1)
        self.jobOutEntry.grid(row=1, column=0, sticky=tk.N+tk.S+tk.W+tk.E)

        self.jobOutEntry.focus_set()

    def copyJobFiles(self):
      if self.selectedJobID != -1:
          job = self.renderJobs[self.selectedJobID]

          if hasattr(self, 'scpWin'):
              self.scpWin.destroy()

          padding = 3

          self.scpWin = tk.Toplevel()
          self.scpWin.resizable(0,0)

          lblSrc = tk.Label(self.scpWin, text='Source:')
          lblSrc.grid(row=0, column=0, padx=padding, pady=padding)

          self.entSrc = tk.Entry(self.scpWin, width=20, state=tk.DISABLED)
          self.entSrc.grid(row=0, column=1, padx=padding, pady=padding)
          self.entSrc.insert(0, job.outputPath)

          modifyDisabledText(self.entSrc, '%s:%s' % (job.host, job.outputPath))

          lblDst = tk.Label(self.scpWin, text='Destination:')
          lblDst.grid(row=1, column=0, padx=padding, pady=padding)

          self.entDst = tk.Entry(self.scpWin, width=20)
          self.entDst.grid(row=1, column=1, padx=padding, pady=padding)
          self.entDst.bind("<Double-Button-1>", lambda event: self.getDirectory(self.entDst, self.scpWin))

          btnOk = ttk.Button(self.scpWin, text='Copy', command = self.verifyCopyJob)
          btnOk.grid(row=2, column=1, padx=padding, pady=padding)

    def verifyCopyJob(self):
        if self.selectedJobID != -1:
            self.logger.info('Verifying copy job...')
            job = self.renderJobs[self.selectedJobID]
            if not job.completed():
                answer = tkmsg.askyesno('Job', 'Job is not yet complete, are you sure you to attempt to copy the files?')
                if not answer:
                    self.logger.info('User cancelled')
                return

            if len(self.entDst.get()) == 0:
                displayError('Invalid option', 'Please enter a destination path')
                return

            dst = os.path.expanduser(self.entDst.get())
            src = os.path.join(job.outputPath, r'%s*{%d..%d}*' % (job.outputPrefix, job.frameRange[0], job.frameRange[1]))

            self.logger.info('Destination : %s\n Source %s' % (dst, src))

            self.scpWin.destroy()

            if not os.path.exists(dst):
                os.makedirs(dst)

            secureCopy(job.host, src, dst, self.logger)
            self.renderJobs[self.selectedJobID].copied = True

    def addJob(self, host, binPath, scenePath, outputPath, frameRange, camOverride=None, resolutionOverride=None):
        args = locals()
        args.pop('self', None)

        logName = os.path.join('~/.rendermanager', 'jobLogs')
        if not os.path.exists(logName):
          os.makedirs(logName)

        args['logPath'] = logName

        self.logger.info('Adding job : \n %s' % json.dumps(args, indent=3))

        try:
            newJob = mayaJob.Job(**args) 
        except IOError, e:
            self.logger.error(e)
            return

        self.renderJobs.append(newJob)
        if newJob.host in self.renderQueue:
            self.renderQueue[newJob.host].append(newJob)
        else:
            self.renderQueue[newJob.host] = [newJob]

        print newJob.state
        print newJob.running
        print newJob.completed()

        time.sleep(5)

        self.jobListbox_list.insert(tk.END, str(self.renderJobs[-1]))
        self.jobListbox_list.selection_clear(0, tk.END)
        self.jobListbox_list.select_set(tk.END)

    def onJobAdd(self):
        self.messageWindow()
        
    def runningJobsOnHost(self, currentJob):
        for job in self.renderJobs:
          print "Check if same job %r" % (job is not currentJob)
          if job is not currentJob:
            print "Check host : %r " % (job.host == currentJob.host)
            if job.host == currentJob.host:
              print "Check if job completed %r" % (not job.completed())
              if not job.completed(): 
                return True
        return False

    def onJobRestart(self):
        if self.selectedJobID != -1:
          newInstance = self.renderJobs[self.selectedJobID].getNewInstanceofJob()

          self.logger.info('Restarting job on host %s' % self.renderJobs[self.selectedJobID].host)

          self.renderJobs[self.selectedJobID].close()
          self.renderJobs[self.selectedJobID] = newInstance

    def onJobRemove(self):
        if self.selectedJobID != -1:
          title = 'Warning'
          message = 'You have not copied over the job files yet, are you sure you want to remove the selected job?'

          if not hasattr(self.renderJobs[self.selectedJobID], 'copied'):
            if not tkmsg.askyesno(title, message):
              return

          self.logger.info('Removing job at id %d' % self.selectedJobID)

          self.cleanlyRemoveJob(self.selectedJobID)
          self.jobListbox_list.delete(self.selectedJobID)

          self.selectedJobID = -1

    def cleanlyRemoveJob(self, id):
        if self.renderJobs[id].errorCode == 0:
          jobLogFile = self.renderJobs[id].jobLogPath
          mayaLogFile = self.renderJobs[id].logPath

          self.logger.info('Removing job log file %s' % jobLogFile)
          self.logger.info('Removing maya log file %s' % mayaLogFile)

          os.remove(jobLogFile)
          os.remove(mayaLogFile)
        else:
          self.logger.info('Job did not end successfully, preserving logs')

        self.renderJobs[id].close()
        del self.renderJobs[id]

    def onJobPauseToggle(self):
        if self.selectedJobID != -1:
          if self.renderJobs[self.selectedJobID].paused:
            self.renderJobs[self.selectedJobID].resume()
          else:
            self.renderJobs[self.selectedJobID].pause()
    
    def onJobKill(self):
        if self.selectedJobID != -1:
          self.renderJobs[self.selectedJobID].kill()

    def onJobSelect(self, val):
        sender = val.widget
        idx = sender.curselection()
        value = sender.get(idx)

        self.selectedJobID = int(idx[0])

        job = self.renderJobs[self.selectedJobID]

        modifyDisabledText(self.entHost, job.host)
        modifyDisabledText(self.entBinPath, job.binaryPath)
        modifyDisabledText(self.entScenePath, job.scenePath)
        modifyDisabledText(self.entFrameRange_1, str(job.frameRange[0]))
        modifyDisabledText(self.entFrameRange_2, str(job.frameRange[0]))
        modifyDisabledText(self.entOutputPath, job.outputPath)
        modifyDisabledText(self.entCameraOverride, job.cameraOverride)
        modifyDisabledText(self.entResOverride_x, job.resolutionOverride[0])
        modifyDisabledText(self.entResOverride_y, job.resolutionOverride[1])
        
    def onExit(self):
        for job in self.renderJobs:
            if not job.completed:
                shouldClose = tkmsg.askyesno('Verify', 'There are still running jobs, do you really want to quit?')
                break
            elif not hasattr(job, 'copied'):
                shouldClose = tkmsg.askyesno('Verify', 'There are still jobs with uncopied files, do you really want to quit?')
                break
        else:
            shouldClose=True

        if shouldClose:
          for id, job in enumerate(self.renderJobs):
            self.cleanlyRemoveJob(id)
          self.onKill()

    def onKill(self, signal=None, frame=None):
        self.logger.info('Application closing...')
        self.shouldExit = True
        if self.updateThread == None:
          self.logger.info('Waiting for background thread to complete')
        else: 
          self.updateThread.cancel()
        self.logger.info('======= Finished ======= ')
        self.parent.destroy()
        #self.parent.quit()
        sys.exit(0)

    def getDirectory(self, output, parent, initialDir=None):
        args = self.dirOpt
        if initialDir:
          self.logger.debug('Setting initial directory to %s' % initialDir)
          args['initialdir'] = initialDir

        dir = tkfile.askdirectory(parent=parent, **args)

        if len(dir)!=0:
          output.delete(0, tk.END)
          output.insert(0, dir)
        else:
          self.logger.error('Empty directory chosen in directory dialogue')
        
    def getFile(self, output, parent, initialDir=None):
        args = self.fileOpt
        if initialDir:
          self.logger.debug('Setting initial file directory to %s' % initialDir)
          args['initialdir'] = initialDir

        dir = tkfile.askopenfilename(parent=parent, **args)
          
        if len(dir)!=0:
          output.delete(0, tk.END)
          output.insert(0, dir)
          self.lastOutput = []
        
    def messageWindow(self):
        if hasattr(self, 'msgWin'):
            # We only want 1 window
            self.msgWin.destroy()
            
        self.msgWin = tk.Toplevel()
        self.msgWin.resizable(0,0)

        btnPad=3

        rowCounter = 1

        # Host
        lblHost = tk.Label(self.msgWin, text="Host Machine : ")
        lblHost.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)
        self.msgWin.rowconfigure(0, weight=1)

        self.iHost = ttk.Combobox(self.msgWin, values=self.hosts)
        self.iHost.grid(row=rowCounter, column=1, sticky='NW')

        rowCounter += 1

        # Exe path
        lblBinPath = tk.Label(self.msgWin, text="Binary Path : ")
        lblBinPath.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)

        self.iBinPath = tk.Entry(self.msgWin, width=20)
        self.iBinPath.bind("<Double-Button-1>", lambda event: self.getDirectory(self.iBinPath, self.msgWin))
        self.iBinPath.grid(row=rowCounter, column=1, sticky='NW')
        self.iBinPath.insert(0, self.defaults['binDir'])

        rowCounter += 1

        # Scene path
        lblScenePath = tk.Label(self.msgWin, text="Scene Path : ")
        lblScenePath.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)

        self.iScenePath = tk.Entry(self.msgWin, width=20)
        self.iScenePath.bind("<Double-Button-1>", lambda event: self.getFile(self.iScenePath, self.msgWin, 
                                                                initialDir=os.path.split(self.workspacePath)[0]))
        self.iScenePath.grid(row=rowCounter, column=1, sticky='NW')

        rowCounter += 1

        # Frame range
        lblFrameRange = tk.Label(self.msgWin, text="Frame range : ")
        lblFrameRange.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)
        frmFrameRange = tk.Frame(self.msgWin)
        frmFrameRange.grid(row=rowCounter, column=1, sticky='NW')

        lblFrameRange_1 = tk.Label(frmFrameRange, text="Start:")
        lblFrameRange_1.grid(row=0, column=0, sticky='NE')
        lblFrameRange_2 = tk.Label(frmFrameRange, text="End:")
        lblFrameRange_2.grid(row=0, column=2, sticky='NE')

        self.iFrameRange_1 = tk.Entry(frmFrameRange, width=5)
        self.iFrameRange_1.insert(0, self.defaults['frames'][0])
        self.iFrameRange_1.grid(row=0, column=1, sticky='NE')
        self.iFrameRange_2 = tk.Entry(frmFrameRange, width=5)
        self.iFrameRange_2.insert(0, self.defaults['frames'][1])
        self.iFrameRange_2.grid(row=0, column=3, sticky='NE')

        rowCounter += 1

        # Camera override
        self.varCameraOverride = tk.IntVar()
        chkCamOverride = tk.Checkbutton(self.msgWin, 
                                        text='Camera override : ', 
                                        variable = self.varCameraOverride, 
                                        command=self.onCamOverrideToggle)
        chkCamOverride.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)

        self.entCamOverride = tk.Entry(self.msgWin)
        self.entCamOverride.grid(row=rowCounter, column=1, sticky='NW', padx=btnPad, pady=btnPad)
        modifyDisabledText(self.entCamOverride, self.defaults['camOverride'])

        self.onCamOverrideToggle()

        rowCounter += 1

        # Resolution override
        self.varResolutionOverride = tk.IntVar()
        chkResolutionOverride = tk.Checkbutton(self.msgWin, 
                                                text='Resolution override : ', 
                                                variable = self.varResolutionOverride, 
                                                command=self.onResOverrideToggle)
        chkResolutionOverride.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)

        frmResolutionOverride = tk.Frame(self.msgWin)
        frmResolutionOverride.grid(row=rowCounter, column=1, sticky='NW')

        lblResolutionOverride_1 = tk.Label(frmResolutionOverride, text="Width:")
        lblResolutionOverride_1.grid(row=0, column=0, sticky='NE')
        lblResolutionOverride_2 = tk.Label(frmResolutionOverride, text="Height:")
        lblResolutionOverride_2.grid(row=0, column=2, sticky='NE')

        self.iResolutionOverride_1 = tk.Entry(frmResolutionOverride, width=5)
        modifyDisabledText(self.iResolutionOverride_1, self.defaults['resolutionOverride'][0])
        self.iResolutionOverride_1.grid(row=0, column=1, sticky='NE')

        self.iResolutionOverride_2 = tk.Entry(frmResolutionOverride, width=5)
        modifyDisabledText(self.iResolutionOverride_2, self.defaults['resolutionOverride'][1])
        self.iResolutionOverride_2.grid(row=0, column=3, sticky='NE')

        self.onResOverrideToggle()

        rowCounter += 1

        # Output path
        lblOutputPath = tk.Label(self.msgWin, text="Output Path (On remote host) : ")
        lblOutputPath.grid(row=rowCounter, column=0, sticky='NE', padx=btnPad, pady=btnPad)

        self.iOutputPath = tk.Entry(self.msgWin, width=20)
        self.iOutputPath.grid(row=rowCounter, column=1, sticky='NW')
        modifyDisabledText(self.iOutputPath, self.defaults['outputDir'])

        rowCounter += 1

        # Verify button #
        btnCheck = ttk.Button(self.msgWin, text="Ok", command=self.verifyNewJob)
        btnCheck.grid(row=rowCounter, column=0, columnspan=2, sticky='N')

    def onCamOverrideToggle(self):
        if self.varCameraOverride.get():
            self.entCamOverride.config(state='normal')
        else:
            self.entCamOverride.config(state='disabled')
            modifyDisabledText(self.entCamOverride, self.defaults['camOverride'])

    def onResOverrideToggle(self):
        if self.varResolutionOverride.get():
            self.iResolutionOverride_1.config(state='normal')
            self.iResolutionOverride_2.config(state='normal')
        else:
            self.iResolutionOverride_1.config(state='disabled')
            self.iResolutionOverride_2.config(state='disabled')
            modifyDisabledText(self.iResolutionOverride_1, self.defaults['resolutionOverride'][0])
            modifyDisabledText(self.iResolutionOverride_2, self.defaults['resolutionOverride'][1])

    def verifyNewJob(self):
        args = { 'host' : self.iHost.get(),
                 'binPath' : self.iBinPath.get(),
                 'scenePath' : self.iScenePath.get(),
                 'frameRange' : [self.iFrameRange_1.get(),self.iFrameRange_2.get()],
                 'outputPath' : self.iOutputPath.get() }

        if self.varCameraOverride.get():
            args['camOverride'] = self.entCamOverride.get()

        if self.varResolutionOverride.get():
            args['resolutionOverride'] = [self.iResolutionOverride_1.get(), self.iResolutionOverride_2.get()]

        if False and not verifyHost(args['host']):
            displayError('Host error', 'Please enter valid host', self.logger)
            return

        if False and not os.path.exists(args['scenePath']):
            displayError('Host error', 'Please enter a valid scene file', self.logger)
            return

        # Check frame range
        for index, type_ in [(0, 'start'), (1, 'end')]:
          if not args['frameRange'][index]:
              displayError('Invalid setting', 'Please enter %s frame' % type_, self.logger)
              return
          else:
              try:
                args['frameRange'][index] = int(args['frameRange'][index])
                if args['frameRange'][index] < 0:
                  displayError('Invalid setting', 'Please enter a positive %s frame' % type_, self.logger)
                  return
              except ValueError:
                displayError('Invalid setting', 'Please enter a valid %s frame' % type_, self.logger)
                return

        if 'resolutionOverride' in args:
            # Check resolution override
            for index, type_ in [(0, 'width'), (1, 'height')]:
                if not args['resolutionOverride'][index]:
                    displayError('Invalid setting', 'Please enter %s  ' % type_, self.logger)
                    return
                else:
                    try:
                        args['resolutionOverride'][index] = int(args['resolutionOverride'][index])
                        if args['resolutionOverride'][index] < 0:
                            displayError('Invalid setting', 'Please enter a positive %s' % type_, self.logger)
                            return
                    except ValueError:
                        displayError('Invalid setting', 'Please enter a valid %s' % type_, self.logger)
                        return

            if args['frameRange'][1] - args['frameRange'][0] < 0:
                displayError('Invalid setting', 'Please enter a positive frame range', self.logger)
                return
            
        for arg in args:
            if arg != 'frameRange' and not args[arg]:
                displayError('Invalid setting', 'Please enter %s'%arg, self.logger)
                return

        self.addJob(**args)
        self.msgWin.destroy()
        

def main():
    root = tk.Tk()
    app = ManagerUI(root)
    root.mainloop()  


if __name__ == '__main__':
    main()  
