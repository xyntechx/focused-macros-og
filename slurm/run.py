#!/usr/bin/env python3

#-------------------------------------------------------------------------
# run
#
# This file simplifies the process of sending jobs to the cluster.
# It parses input arguments that describe how the jobs should be
# submitted, writes a bash script to a file, and finally calls qsub
# with that bash script as an argument.
#
# When slurm runs the script, the first thing it does is source a
# virtualenv script that configures the python environment properly.
#-------------------------------------------------------------------------

import argparse
import datetime
import os
import re
import subprocess
import sys
import time

defaultjob = 'run'

def parse_args():
    # Parse input arguments
    #   Use --help to see a pretty description of the arguments
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--command', help='The command to run (e.g. "python -m module.name --arg=value")', type=str, required=True)
    parser.add_argument('--jobname', help='A name for the job (max 10 chars)', type=str, default=defaultjob)
    parser.add_argument('--cpus', help='Number of CPUs to request', type=int, default=1)
    parser.add_argument('--gpus', help='Number of GPUs to request', type=int, default=0)
    parser.add_argument('--mem', help='Amount of RAM to request *per node* (in GB)', type=int, default=2)
    parser.add_argument('--env', help='Path to virtualenv', type=str, default='./env')
    parser.add_argument('--duration', help='Expected duration of job', choices=['test', 'short', 'long', 'vlong'], default='vlong')
    parser.add_argument('--host', help='Wildcard for targeting a specific host or set of hosts', type=str, default=None)
    parser.add_argument('-t','--taskid', help='Task ID of first task', type=int, default=1)
    parser.add_argument('--tasklist', help='Comma separated list of task IDs to submit (e.g. "18-22:1,26,29,34-49:1")', type=str, default=None)
    parser.add_argument('-n','--ntasks', help='Number of tasks', type=int, default=0)
    parser.add_argument('-max','--maxtasks', help='Maximum number of simultaneous tasks', type=int, default=-1)
    parser.add_argument('-y','--dry_run', help="Don't actually submit jobs to slurm", action='store_true')
    parser.set_defaults(dry_run=False)
    parser.add_argument('--email', help='Email address(es) to notify when job is complete: addr1@brown.edu[, addr2@brown.edu]', type=str, default=None)
    parser.add_argument('--hold_jid', help='Hold job until the specified jobid or jobid_taskid has finished', type=str, default=None)
    return parser.parse_args()
args = parse_args()

def run():
    # Define the bash script that qsub should run (with values
    # that need to be filled in using the input args).
    venv_path = os.path.join(args.env, 'bin', 'activate')
    script_body='''#!/bin/bash

module load python/3.7.4
module load cuda/9.2.148
source {}
{} '''.format(venv_path, args.command)

    if args.ntasks > 0 or args.tasklist is not None:
        script_body += r'$SLURM_ARRAY_TASK_ID'
    script_body += '\n'

    # Write the script to a file
    os.makedirs("grid/scripts/", exist_ok=True)
    jobfile = "grid/scripts/{}".format(args.jobname)
    with open(jobfile, 'w') as f:
        f.write(script_body)

    # Call the appropriate sbatch command. The default behavior is to use
    # Slurm's job array feature, which starts a batch job with multiple tasks
    # and passes a different taskid to each one. If ntasks is zero, only a
    # single job is submitted with no subtasks.
    cmd = 'sbatch '
    # Slurm runs scripts in current working directory by default

    # Duration
    if args.duration == 'test':
        cmd += '-t 0:10:00 ' # 10 minutes
    elif args.duration == 'short':
        cmd += '-t 1:00:00 ' # 1 hour
    elif args.duration == 'long':
        cmd += '-t 1-00:00:00 '# 1 day
    elif args.duration == 'vlong':
        cmd += '-t 7-00:00:00 '# 1 week

    # Number of CPU/GPU resources
    cmd += '-n {} '.format(args.cpus)
    if args.gpus > 0:
        partition = 'gpu-debug' if args.duration in ['test','short'] else 'gpu'
        cmd += '-p {} --gres=gpu:{}'.format(partition, args.gpus)
    else:
        partition = 'debug' if args.duration in ['test','short'] else 'batch'

    # Memory requirements
    cmd += '--mem={}G '.format(args.mem)

    # Force a specific set of hosts
    if args.host is not None:
        cmd += '-q {}.q@{}.cs.brown.edu '.format(args.duration, args.host)

    # Logging
    os.makedirs("./grid/logs/", exist_ok=True)
    cmd += '-o ./grid/logs/{}.o%A.%a '.format(args.jobname) # save stdout to file
    cmd += '-e ./grid/logs/{}.e%A.%a '.format(args.jobname) # save stderr to file

    # The -terse flag causes qsub to print the jobid to stdout. We read the
    # jobid with subprocess.check_output(), and use it to delay the email job
    # until the entire batch job has completed.
    cmd += '--parsable '

    if args.ntasks > 0:
        assert args.tasklist is None, 'Arguments ntasks and tasklist not supported simultaneously.'
        cmd += "--array={}-{}".format(args.taskid, args.taskid+args.ntasks-1) # specify task ID range
        if args.maxtasks > 0:
            cmd += '%{}'.format(args.maxtasks) # set maximum number of running tasks
        else:
            cmd += ' '
    elif args.tasklist is not None:
        cmd += "--array={taskblock} "
    else:
        pass

    # Prevent Slurm from running this new job until the specified job ID is finished.
    if args.hold_jid is not None:
        cmd += "--depend=afterany:{} ".format(args.hold_jid)
    cmd += "{}".format(jobfile)

    def launch(cmd):
        print(cmd)
        if not args.dry_run:
            try:
                byte_str = subprocess.check_output(cmd, shell=True)
                jobid = int(byte_str.decode('utf-8').split('.')[0])
                if args.email is not None:
                    notify_cmd = 'qsub '
                    notify_cmd += '-o /dev/null ' # don't save stdout file
                    notify_cmd += '-e /dev/null ' # don't save stderr file
                    notify_cmd += '--mail-type=BEGIN' # send email when this new job starts
                    notify_cmd += '--mail-user="{}" '.format(args.email) # email address
                    notify_cmd += '--depend=afterany:{} '.format(jobid)
                    notify_cmd += '-J ~{} '.format(args.jobname[1:]) # modify the jobname slightly
                    notify_cmd += '--wrap="sleep 0"' # the actual job is a NO-OP
                    subprocess.call(notify_cmd, shell=True)
            except (subprocess.CalledProcessError, ValueError) as e:
                print(e)
                sys.exit()

    if args.tasklist is None:
        launch(cmd)
    else:
        taskblocks = args.tasklist.split(',')
        for taskblock in taskblocks:
            launch(cmd.format(taskblock=taskblock))

if args.jobname == defaultjob:
    args.jobname = "run{}".format(args.taskid)
elif not re.match(r'^(\w|\.)+$', args.jobname):
    # We want to create a script file, so make sure the filename is legit
    print("Invalid job name: {}".format(args.jobname))
    sys.exit()
run()
