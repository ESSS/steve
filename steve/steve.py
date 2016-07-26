from __future__ import unicode_literals, print_function, division

# TODO: stuff that can be implemented/improved
# * configure polling interval
# * use a config file?
# * attempt few times before giving up on requests that fail
# * better feedback when queued
# * improve code/docs
# * improve how text is colored
# * possibly accept commands to abort jobs

import argparse
import json
import logging
import os
from getpass import getpass

import math

import requests
import subprocess

import sys

import time

import trollius
import yaml
from concurrent.futures.thread import ThreadPoolExecutor

from .version import __version__

ALL_PLATFORMS = ['win32', 'win32d', 'win64', 'win64d', 'linux64']

ESTIMATE_UNRELIABILITY = 1.25
WATCH_INTERVAL = 10
PRINT_INTERVAL = 1


def run(args):
    parser = argparse.ArgumentParser(
        description='A tool that simplifies running Jenkins jobs from '
                    'command-line')
    parser.add_argument(
        '-u', '--user', required=True, help='Username')
    parser.add_argument(
        '-b', '--branch', default=None, help='Branch of the CI job to trigger '
                                             '(defaults to current branch)')
    parser.add_argument(
        '-m', '--mode', default=None, help='A mode, if not provided gets first '
                                           'mode from .jobs_done.yaml (if'
                                           ' any mode exists)')
    parser.add_argument(
        '-p', '--platforms', nargs='+', help='Platforms separated by space, '
                                             'if not provided uses current '
                                             'platform')
    parser.add_argument(
        '-v', '--version',
        action='version', version='%(prog)s {}'.format(__version__))
    args = parser.parse_args(args=args)

    password = getpass()

    full_repo = subprocess.check_output(
            'git rev-parse --show-toplevel', shell=True).strip()
    repo = os.path.basename(full_repo)

    branch = args.branch
    if not branch:
        branch = subprocess.check_output(
            'git rev-parse --abbrev-ref HEAD', shell=True).strip()

    mode = args.mode
    if not mode:
        modes = read_modes_from_jobs_done(
            os.path.join(full_repo, '.jobs_done.yaml'))
        mode = modes[0] if modes else None

    if args.platforms:
        platforms = args.platforms
    else:
        plat_os = 'win' if sys.platform.startswith('win') else 'linux'
        plat_arch = os.environ['architecture'] if sys.platform.startswith('win') else 64
        plat_debug = 'd' if hasattr(sys, 'gettotalrefcount') else ''
        platforms = ['{}{}{}'.format(plat_os, plat_arch, plat_debug)]

    invalid_platforms = [p for p in platforms if p not in ALL_PLATFORMS]
    if invalid_platforms:
        raise ValueError('Invalid platforms: {}'.format(
            ','.join(invalid_platforms)))

    request_args = dict(
        branch=branch,
        name=repo,
        mode=mode,
    )

    jobs = Jobs(user=args.user, password=password, platforms=platforms)

    loop = trollius.get_event_loop()
    # Network I/O bound tasks, unnecessary to be conservative with # of threads
    loop.set_default_executor(ThreadPoolExecutor(max_workers=32))

    printer_task = loop.create_task(
        printer(jobs=jobs, branch=branch, mode=mode, platforms=platforms))

    watcher_tasks = []
    for platform in platforms:
        watcher_tasks.append(loop.create_task(
            watcher(jobs=jobs, platform=platform, request_args=request_args)
        ))

    tasks = [printer_task] + watcher_tasks
    done, pending = loop.run_until_complete(trollius.wait(tasks))
    loop.close()

    if any(t.exception() is not None for t in done):
        exit(1)


class Jobs:

    # To discover available attributes in Jenkins API, browse an URL similar to
    # https://eden.esss.com.br/jenkins/job/xmera-fb-xmera-jobs-win64/lastBuild/api/xml
    # and see what is available for the build. Note you can exchange xml by
    # json to switch the output format of API.
    BUILD_URL = 'https://eden.esss.com.br/jenkins/job/{job_name}/build?delay=0sec'
    PROGRESS_URL = 'http://eden.esss.com.br/jenkins/job/{job_name}/{job_id}/api/json?tree=id,timestamp,estimatedDuration,building'
    RESULT_URL = 'http://eden.esss.com.br/jenkins/job/{job_name}/{job_id}/api/json?tree=result'

    STATUS_UNKNOWN = 'unknown'
    STATUS_BUILDING = 'building'
    STATUS_SUCCESS = 'success'
    STATUS_FAILURE = 'failure'
    STATUS_ABORT = 'abort'
    STATUS_CONNECTION_ERROR = 'connection_error'
    STATUS_INTERNAL_ERROR = 'internal_error'

    def __init__(self, user, password, platforms):
        self.user = user
        self.password = password
        self.platforms = platforms
        self.done = {p: False for p in self.platforms}
        self.status = {p: self.STATUS_UNKNOWN for p in self.platforms}
        self.progress = {p: 0. for p in self.platforms}
        self.duration = {p: None for p in self.platforms}

    @trollius.coroutine
    def build(self, request_args):
        build_url = self.BUILD_URL.format(**self.process_request_args(request_args))
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, build_url)
        raise trollius.Return(r)

    @trollius.coroutine
    def monitor(self, request_args):
        progress_url = self.PROGRESS_URL.format(**self.process_request_args(request_args))
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, progress_url)
        raise trollius.Return(r)

    @trollius.coroutine
    def result(self, request_args):
        progress_url = self.RESULT_URL.format(**self.process_request_args(request_args))
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, progress_url)
        raise trollius.Return(r)

    def send_request(self, url):
        return requests.post(url, auth=(self.user, self.password))

    def is_request_ok(self, response):
        return response.status_code in [requests.codes.ok, requests.codes.created]

    def process_request_args(self, request_args):
        mode = request_args['mode']
        if mode is None:
            job_name = '{name}-{branch}-{platform}'
        else:
            job_name = '{name}-{branch}-{mode}-{platform}'

        return dict(job_name=job_name.format(**request_args), **request_args)


@trollius.coroutine
def watcher(jobs, platform, request_args):
    try:
        # While it doesn't know which job exactly it is going to watch, look
        # for most current build
        platform_args = dict(job_id='lastBuild', platform=platform, **request_args)

        # 1. monitor progress until stops building
        waiting = True
        building = False
        job_id = None
        while waiting or building:
            logging.debug("[{}] request to watch build".format(platform))
            monitor_ret = yield trollius.From(jobs.monitor(platform_args))
            logging.debug("[{}] response to watch build is {}".format(platform, jobs.is_request_ok(monitor_ret)))

            if jobs.is_request_ok(monitor_ret):
                progress_content = json.loads(monitor_ret.content)
                job_id = progress_content['id']
                timestamp = progress_content['timestamp']  # in milliseconds
                estimated = progress_content['estimatedDuration']
                building = progress_content['building']
                logging.debug("[{}] watch response content: {}, {}, {}".format(platform, building, timestamp, estimated))
            elif monitor_ret.status_code == requests.codes.not_found:
                # If not found, it may mean it is first time there is a build
                # for this job.
                logging.debug("[{}] watch found no builds, will try to create build".format(platform))
            else:
                jobs.status[platform] = jobs.STATUS_CONNECTION_ERROR
                raise trollius.Return()

            if waiting and not building:
                # If not building yet, request to start a build
                logging.debug("[{}] request to build".format(platform))
                build_ret = yield trollius.From(jobs.build(platform_args))
                logging.debug("[{}] response to build is {}".format(platform, jobs.is_request_ok(build_ret)))
                if not jobs.is_request_ok(build_ret):
                    jobs.status[platform] = jobs.STATUS_CONNECTION_ERROR
                    raise trollius.Return()
            else:
                # Once build is running, follow especially this job. This avoids
                # external restart of job messing with this watcher
                platform_args = dict(
                    job_id=job_id, platform=platform, **request_args)
                waiting = False
                fixed_timestamp = time.time() - timestamp / 1000

                # Just save first duration, as printer is a lot more frequent
                # than watchers, so it is better to increment duration there for
                # better feedback
                if jobs.duration[platform] is None:
                    jobs.duration[platform] = fixed_timestamp

                if estimated > 0:
                    jobs.status[platform] = jobs.STATUS_BUILDING
                    # Jenkins estimate not very reliable, be conservative
                    fixed_estimated = ESTIMATE_UNRELIABILITY * estimated / 1000.
                    progress = fixed_timestamp / fixed_estimated
                    # Even with some unreliability factor, progress can
                    # exceed 100%
                    jobs.progress[platform] = min(progress, 0.99)
                else:
                    jobs.status[platform] = jobs.STATUS_UNKNOWN

            yield trollius.sleep(WATCH_INTERVAL)

        # 3. once stopped building, one last request to get final status of job
        logging.debug("[{}] request to get result".format(platform))
        result_ret = yield trollius.From(jobs.result(platform_args))
        logging.debug("[{}] response to get result is {}".format(platform, jobs.is_request_ok(result_ret)))
        if not jobs.is_request_ok(result_ret):
            jobs.status[platform] = jobs.STATUS_CONNECTION_ERROR
            raise trollius.Return()

        result = json.loads(result_ret.content)
        status = result['result']

        if status == 'FAILURE':
            jobs.status[platform] = jobs.STATUS_FAILURE
        elif status == 'ABORTED':
            jobs.status[platform] = jobs.STATUS_ABORT
        elif status == 'SUCCESS':
            jobs.status[platform] = jobs.STATUS_SUCCESS
        else:
            assert False, "Could not parse status {}".format(status)
    except trollius.coroutines.ReturnException:
        pass
    except:
        jobs.status[platform] = jobs.STATUS_INTERNAL_ERROR
        logging.exception("Internal error while trying to watch job progress")
    finally:
        jobs.done[platform] = True


@trollius.coroutine
def printer(jobs, branch, mode, platforms):
    progress_width = 50
    total_width = 120
    count = len(platforms) + 1
    feedback = 0

    def write_line(line):
        diff = max(total_width - len(line), 0)
        print('{}{}'.format(line, ' ' * diff))

    sys.stdout.write("\n" * count)  # Make sure we have space to draw the bars

    # Use its own done status, to make sure each print all status found
    done = {p: False for p in platforms}

    name_width = max(len(p) for p in platforms)

    while not all(done.itervalues()):
        sys.stdout.write("\u001b[1000D")  # Move left
        sys.stdout.write("\u001b[{}A".format(count))  # Move up

        if mode:
            write_line("Monitoring jobs in branch \u001b[33m{}\u001b[0m and mode \u001b[33m{}\u001b[0m for platforms \u001b[33m{}\u001b[0m {}".format(branch, mode, ", ".join(platforms), '.' * (feedback + 1)))
        else:
            write_line("Monitoring jobs in branch \u001b[33m{}\u001b[0m for platforms \u001b[33m{}\u001b[0m {}".format(branch, ", ".join(platforms), '.' * (feedback + 1)))
        feedback = (feedback + 1) % 3
        for platform in platforms:
            pretty_platform = platform.ljust(name_width)
            status = jobs.status[platform]
            duration = jobs.duration[platform]
            if duration is not None:
                pretty_duration = '{: 3d}:{:02d}'.format(int(duration // 60), int(math.floor(duration % 60)))
                if not jobs.done[platform]:
                    jobs.duration[platform] += PRINT_INTERVAL
            else:
                pretty_duration = '?'

            if status == jobs.STATUS_UNKNOWN:
                write_line("{}: [{}] {} / {}".format(pretty_platform, "unknown".center(progress_width, " "), "?", pretty_duration))
            elif status == jobs.STATUS_BUILDING:
                progress = jobs.progress[platform]
                width = int(math.ceil(progress * progress_width))
                write_line("{}: [{}{}] {} / {}".format(pretty_platform, "#" * width, " " * (progress_width - width), "{: 3d}%".format(int(round(progress * 100))), pretty_duration))
            elif status == jobs.STATUS_SUCCESS:
                write_line("{}: [{}] {} / {}".format(pretty_platform, "#" * progress_width, "\u001b[32m{}\u001b[0m".format('success'), pretty_duration))
                done[platform] = True
            elif status == jobs.STATUS_ABORT:
                progress = jobs.progress[platform]
                width = int(math.ceil(progress * progress_width))
                write_line("{}: [{}{}] {} / {}".format(pretty_platform, "#" * width, " " * (progress_width - width), "\u001b[33m{}\u001b[0m".format('aborted'), pretty_duration))
                done[platform] = True
            elif status in (jobs.STATUS_FAILURE, jobs.STATUS_INTERNAL_ERROR, jobs.STATUS_CONNECTION_ERROR):
                progress = jobs.progress[platform]
                width = int(math.ceil(progress * progress_width))
                caption = {
                    jobs.STATUS_FAILURE: 'failed',
                    jobs.STATUS_INTERNAL_ERROR: 'internal error',
                    jobs.STATUS_CONNECTION_ERROR: 'connection error',
                }[status]
                write_line("{}: [{}{}] {} / {}".format(pretty_platform, "#" * width, " " * (progress_width - width), "\u001b[31m{}\u001b[0m".format(caption), pretty_duration))
                done[platform] = True
            else:
                assert False, "unknown status"

        yield trollius.sleep(PRINT_INTERVAL)


def read_modes_from_jobs_done(filename):
    with open(filename, 'r') as f:
        yml = yaml.safe_load(f)
        return yml.get('matrix').get('mode', [])
