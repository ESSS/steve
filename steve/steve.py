from __future__ import unicode_literals, print_function, division

import abc
import bisect
import json
import logging
import math
import os
import subprocess
import sys
import time
from getpass import getpass

import requests
import trollius
import yaml
from concurrent.futures.thread import ThreadPoolExecutor

ALL_PLATFORMS = ['win32',
                 'win32d',
                 'win32g',
                 'win64',
                 'win64d',
                 'win64g',
                 'linux64',
                 'none',
                 'promote']

ESTIMATE_UNRELIABILITY = 1.25
WATCH_INTERVAL = 5
PRINT_INTERVAL = 1


def run(user, branch, matrix, configuration, debug=False, parameters=None):
    # TODO: stuff that can be implemented/improved
    # * configure polling interval
    # * attempt few times before giving up on requests that fail
    # * better feedback when queued
    # * improve code/docs
    # * improve how text is colored
    # * fix duration (initial value read from remote isn't correct right now)
    # * allow default matrix configuration in config file

    if debug:
        # Set it to enable debug feature of trollius
        os.environ['TROLLIUSDEBUG'] = '1'

        # Uncomment and configure for logging
        logger = logging.getLogger()
        logger.setLevel(logging.DEBUG)

        if sys.platform.startswith('win'):
            log_file = os.path.join(os.path.expanduser('~'), 'steve.log')
        else:
            log_file = os.path.join(os.path.expanduser('~'), '.steve.log')
        logger.addHandler(logging.FileHandler(log_file))

    password = (user == configuration.user and configuration.password) or \
        getpass()

    full_repo = subprocess.check_output('git rev-parse --show-toplevel', shell=True).strip()
    repo = os.path.basename(full_repo)

    if not branch:
        branch = subprocess.check_output('git rev-parse --abbrev-ref HEAD', shell=True).strip()

    full_matrix = read_jobs_done_matrix(os.path.join(full_repo, '.jobs_done.yaml'))
    all_combinations = list(iter_jobs_combinations(full_matrix))
    default_configuration = all_combinations[0]

    if not matrix:
        configuration = default_configuration.copy()
        fix_platform = True

        configurations = [configuration]
    else:
        matrix = parse_matrix_from_args(matrix, full_matrix)
        fix_platform = 'platform' not in matrix
        fill_missing_in_matrix(matrix, default_configuration)

        configurations = list(iter_jobs_combinations(matrix))

    if fix_platform:
        # Let platform of current terminal prevail as it is usually the one that is going to
        # be preferred by developers rather than one in jobs_done YAML.
        plat_os = 'win' if sys.platform.startswith('win') else 'linux'
        plat_arch = os.environ['architecture'] if sys.platform.startswith('win') else 64
        plat_debug = 'd' if hasattr(sys, 'gettotalrefcount') else ''
        platform = '{}{}{}'.format(plat_os, plat_arch, plat_debug)
        for configuration in configurations:
            configuration['platform'] = platform

    platforms = get_platforms_from_comfigurations(configurations)
    invalid_platforms = [p for p in platforms if p not in ALL_PLATFORMS]
    if invalid_platforms:
        raise ValueError('Invalid platforms: {}'.format(
            ','.join(invalid_platforms)))

    if parameters:
        parameters = parse_parameters_from_args(parameters)
    jobs = BuildJobs(
        user=user, password=password, configurations=configurations, branch=branch, name=repo,
        parameters=parameters)

    loop = trollius.get_event_loop()
    # Network I/O bound tasks, unnecessary to be conservative with # of threads
    loop.set_default_executor(ThreadPoolExecutor(max_workers=32))

    printer_task = loop.create_task(printer(jobs=jobs))

    watcher_tasks = []
    for job in jobs.instances:
        watcher_tasks.append(loop.create_task(watcher(job=job)))

    tasks = [printer_task] + watcher_tasks
    done = []
    error_code = 0
    try:
        done, pending = loop.run_until_complete(trollius.wait(tasks))
    except KeyboardInterrupt:
        try:
            answer = raw_input("Abort jobs [Y/y] or press any key to exit (keeps jobs running) ")
        except KeyboardInterrupt:
            pass
        else:
            if answer.lower() == 'y':
                aborter_tasks = [loop.create_task(aborter(job)) for job in jobs.instances]
                tasks = [printer_task] + aborter_tasks
                done, pending = loop.run_until_complete(trollius.wait(tasks))
    finally:
        loop.close()
        if any(t.exception() is not None for t in done):
            error_code = 1

    sys.stdout.write("\n")
    return error_code


class BuildJobs:

    def __init__(self, user, password, configurations, branch, name, parameters):
        self.user = user
        self.password = password
        self.configurations = configurations
        self.branch = branch
        self.name = name
        self.platforms = get_platforms_from_comfigurations(configurations)
        self.parameters = parameters

        self.instances = [
            BuildJob(
                user=user,
                password=password,
                configuration=configuration,
                branch=branch,
                name=name,
                parameters=parameters,
            )
            for configuration in self.configurations]

    def get_job_by_platform(self, platform):
        i = bisect.bisect(self.platforms, platform)
        return self.instances[i]


STATUS_UNKNOWN = 'unknown'
STATUS_BUILDING = 'building'
STATUS_SUCCESS = 'success'
STATUS_FAILURE = 'failure'
STATUS_ABORT = 'abort'
STATUS_CONNECTION_ERROR = 'connection_error'
STATUS_INTERNAL_ERROR = 'internal_error'

# To discover available attributes in Jenkins API, browse an URL similar to
# https://eden.esss.com.br/jenkins/job/xmera-fb-xmera-jobs-win64/lastBuild/api/xml
# and see what is available for the build. Note you can exchange xml by
# json to switch the output format of API.
JENKINS_URL = 'https://eden.esss.com.br/jenkins/'


def is_request_ok(response):
    return response.status_code in [requests.codes.ok, requests.codes.created]


def is_request_not_found(response):
    return response.status_code == requests.codes.not_found


class Job:
    __metaclass__ = abc.ABCMeta

    def __init__(self, user, password):
        self.user = user
        self.password = password

    def send_request(self, url, parameters=None):
        return requests.post(url, auth=(self.user, self.password), data=parameters)


class BuildJob(Job):

    JOB_URL = JENKINS_URL + 'job/{job_name}'
    JOB_WITH_ID_URL = '{job_url}/{job_id}'
    BUILD_URL = '{job_url}/build?delay=0sec'
    BUILD_WITH_PARAM_URL = '{job_url}/buildWithParameters?delay=0sec'
    ABORT_URL = JOB_WITH_ID_URL + '/stop'
    BUILD_NUMBER_URL = JOB_WITH_ID_URL + '/buildNumber'
    PROGRESS_URL = JOB_WITH_ID_URL + '/api/json?tree=id,timestamp,estimatedDuration,building,result'
    RESULT_URL = JOB_WITH_ID_URL + '/api/json?tree=result'

    def __init__(self, user, password, configuration, branch, name, parameters):
        Job.__init__(self, user, password)
        self.name = name
        self.branch = branch
        self.configuration = configuration
        self.parameters = parameters

        self.done = False
        self.cancelled = False
        self.status = STATUS_UNKNOWN
        self.progress = 0.
        self.duration = None
        # While it doesn't know which job exactly it is going to watch, look
        # for most current build
        self.job_id = '1'

    @property
    def platform(self):
        return self.configuration['platform']

    @property
    def url(self):
        return self.get_job_with_id_url()

    def update_job_id(self, job_id):
        self.job_id = job_id

    @trollius.coroutine
    def get_last_build_id(self):
        job_url = self.get_job_url()
        build_number_url = self.BUILD_NUMBER_URL.format(job_url=job_url, job_id='lastCompletedBuild')
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, build_number_url, self.parameters)
        raise trollius.Return(r)

    @trollius.coroutine
    def build(self):
        job_url = self.get_job_url()
        build_url = self.BUILD_WITH_PARAM_URL if self.parameters else self.BUILD_URL
        build_url = build_url.format(job_url=job_url)
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, build_url, self.parameters)
        raise trollius.Return(r)

    @trollius.coroutine
    def monitor(self):
        job_url = self.get_job_url()
        progress_url = self.PROGRESS_URL.format(job_id=self.job_id, job_url=job_url)
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, progress_url)
        raise trollius.Return(r)

    @trollius.coroutine
    def result(self):
        job_url = self.get_job_url()
        progress_url = self.RESULT_URL.format(job_id=self.job_id, job_url=job_url)
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, progress_url)
        raise trollius.Return(r)

    @trollius.coroutine
    def abort(self):
        job_url = self.get_job_url()
        abort_url = self.ABORT_URL.format(job_id=self.job_id, job_url=job_url)
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, abort_url)
        raise trollius.Return(r)

    def get_job_name(self):
        # See `jobs_done10` repo (https://eden.esss.com.br/stash/projects/ESSS/repos/jobs_done10/)
        # for more details about how branchs are named.
        #
        # The gist of it is:
        # * first is project name
        # * next comes the branch name
        # * after them, come all matrix values, sorted alphabetically by their matrix keys
        job_name = '{name}-{branch}-'.format(name=self.name, branch=self.branch)
        job_name += self.get_job_short_name()
        return job_name

    def get_job_short_name(self):
        values = (self.configuration[key] for key in sorted(self.configuration.keys()))
        return '-'.join(values)

    def get_job_url(self):
        job_name = self.get_job_name()
        return self.JOB_URL.format(job_name=job_name)

    def get_job_with_id_url(self):
        return self.JOB_WITH_ID_URL.format(
            job_url=self.get_job_url(),
            job_id=self.job_id,
        )


class QueueJob(Job):

    DEQUEUE_URL = JENKINS_URL + 'queue/cancelItem?id={queue_item_id}'
    QUEUE_ITEMS_URL = JENKINS_URL + 'queue/api/json?tree=items[id,task[name]]'

    @trollius.coroutine
    def dequeue(self, queue_item_id):
        dequeue_url = self.DEQUEUE_URL.format(queue_item_id=queue_item_id)
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, dequeue_url)
        raise trollius.Return(r)

    @trollius.coroutine
    def queue_items(self):
        queue_items_url = self.QUEUE_ITEMS_URL
        loop = trollius.get_event_loop()
        r = yield loop.run_in_executor(None, self.send_request, queue_items_url)
        raise trollius.Return(r)


@trollius.coroutine
def aborter(job):
    short_name = job.get_job_short_name()
    if job.status not in (STATUS_BUILDING, STATUS_UNKNOWN):
        logging.debug("BuildJob [{}] has status {}, so not aborting it", short_name, job.status)
        raise trollius.Return()

    # To make sure it stops watching and retrying to build
    job.cancelled = True

    if job.status == STATUS_UNKNOWN:
        queue_job = QueueJob(job.user, job.password)
        logging.info("Dequeuing job [{}]...", short_name)

        queue_items_ret = yield trollius.From(queue_job.queue_items())
        if not is_request_ok(queue_items_ret):
            # Error requesting Jenkins queue
            job.status = STATUS_CONNECTION_ERROR

        else:
            queue_ids_content = json.loads(queue_items_ret.content)

            # Search for our job in the Jenkins queue
            for queue_item in queue_ids_content['items']:
                job_name = queue_item['task']['name']
                if job_name == job.get_job_name():
                    queue_item_id = queue_item['id']
                    break
            else:
                # This could happen if the job started between us requesting the queue and
                # parsing the job id. It should be a very rare situation. Probably not worth
                # handling.
                raise trollius.Return()

            logging.debug("[{}] request to dequeue".format(short_name))
            dequeue_ret = yield trollius.From(queue_job.dequeue(queue_item_id))
            # Dequeue request has the terrible habit of returning 404 error code even when it
            # successfully pops build from queue...
            dequeue_ok = is_request_ok(dequeue_ret) or is_request_not_found(dequeue_ret)
            logging.debug("[{}] response to dequeue is {}".format(short_name, dequeue_ok))

            if not dequeue_ok:
                # error dequeuing
                job.status = STATUS_CONNECTION_ERROR

    else:
        logging.info("Aborting job [{}]...", short_name)

        logging.debug("[{}] request to abort".format(short_name))
        abort_ret = yield trollius.From(job.abort())
        logging.debug("[{}] response to abort is {}".format(short_name, is_request_ok(abort_ret)))

        if not is_request_ok(abort_ret):
            job.status = STATUS_CONNECTION_ERROR

        print("Go to {} for aborted build in platform \u001b[33m{}\u001b[0m".format(
            job.url, short_name))


@trollius.coroutine
def watcher(job):
    try:
        short_name = job.get_job_short_name()

        # 1. monitor progress until stops building
        waiting = True
        building = False
        result = 'null'
        while not job.cancelled and (waiting or building):
            logging.debug("[{}] request to watch build".format(short_name))
            monitor_ret = yield trollius.From(job.monitor())
            if job.cancelled:
                break
            logging.debug("[{}] response to watch build is {}".format(
                short_name, is_request_ok(monitor_ret)))

            if is_request_ok(monitor_ret):
                progress_content = json.loads(monitor_ret.content)
                timestamp = progress_content['timestamp']  # in milliseconds
                estimated = progress_content['estimatedDuration']
                building = progress_content['building']
                result = progress_content['result']
                logging.debug("[{}] watch response content: {}, {}, {}".format(
                    short_name, building, timestamp, estimated))
            elif monitor_ret.status_code == requests.codes.not_found:
                # If not found, it may mean it is first time there is a build
                # for this job.
                logging.debug("[{}] watch found no builds, will try to create build".format(
                    short_name))
            else:
                job.status = STATUS_CONNECTION_ERROR
                raise trollius.Return()

            if waiting and not building and result == 'null':
                # First we need to predict next build number
                logging.debug("[{}] get last build id".format(short_name))
                last_build_id_ret = yield trollius.From(job.get_last_build_id())
                if is_request_ok(last_build_id_ret):
                    last_build_id = last_build_id_ret.text.strip()
                    logging.debug("[{}] previous build id is {}".format(short_name, last_build_id))
                    next_build_id = '{}'.format(int(last_build_id) + 1)
                    logging.debug("[{}] next build id is {}".format(short_name, next_build_id))
                    job.update_job_id(next_build_id)
                else:
                    logging.debug("[{}] get last build id failed. Maybe first build?".format(short_name))

                # If not building yet, request to start a build
                logging.debug("[{}] request to build".format(short_name))
                build_ret = yield trollius.From(job.build())
                if job.cancelled:
                    break
                logging.debug("[{}] response to build is {}".format(short_name, is_request_ok(build_ret)))
                if not is_request_ok(build_ret):
                    job.status = STATUS_CONNECTION_ERROR
                    raise trollius.Return()
            else:
                # Once build is running, follow especially this job. This avoids
                # external restart of job messing with this watcher
                waiting = False
                fixed_timestamp = time.time() - timestamp / 1000

                # Just save first duration, as printer is a lot more frequent
                # than watchers, so it is better to increment duration there for
                # better feedback
                if job.duration is None:
                    job.duration = fixed_timestamp

                if estimated > 0:
                    job.status = STATUS_BUILDING
                    # Jenkins estimate not very reliable, be conservative
                    fixed_estimated = ESTIMATE_UNRELIABILITY * estimated / 1000.
                    progress = fixed_timestamp / fixed_estimated
                    # Even with some unreliability factor, progress can
                    # exceed 100%
                    job.progress = min(progress, 0.99)
                else:
                    job.status = STATUS_UNKNOWN

            yield trollius.sleep(WATCH_INTERVAL)

        if job.cancelled:
            # 3.1 if cancelled just mark as aborted
            job.status = STATUS_ABORT
        else:
            # 3.2 once stopped building, one last request to get final status of job
            logging.debug("[{}] request to get result".format(short_name))
            result_ret = yield trollius.From(job.result())
            logging.debug(
                "[{}] response to get result is {}".format(short_name, is_request_ok(result_ret)))
            if not is_request_ok(result_ret):
                job.status = STATUS_CONNECTION_ERROR
                raise trollius.Return()

            result = json.loads(result_ret.content)
            status = result['result']

            if status == 'FAILURE':
                job.status = STATUS_FAILURE
            elif status == 'ABORTED':
                job.status = STATUS_ABORT
            elif status == 'SUCCESS':
                job.status = STATUS_SUCCESS
            else:
                assert False, "Could not parse status {}".format(status)
    except Exception as e:
        if not isinstance(e, trollius.coroutines.Return):
            job.status = STATUS_INTERNAL_ERROR
            logging.exception("Internal error while trying to watch job progress")
        else:
            raise e
    finally:
        job.done = True


@trollius.coroutine
def printer(jobs):
    progress_width = 50
    total_width = get_terminal_width()
    feedback = 0
    failed = set()

    def write_line(line):
        diff = max(total_width - len(line), 0)
        print('{}{}'.format(line, ' ' * diff))

    def count_lines():
        # Cancel prompts a line to user, after that point onward that has to be taken in
        # account
        cancelled = 0
        if any(j.cancelled for j in jobs.instances):
            cancelled = 1
        return len(jobs.instances) + 1 + len(failed) + cancelled

    sys.stdout.write("\n" * count_lines())  # Make sure we have space to draw the bars

    # Use its own done status, to make sure each print all status found
    done = {j: False for j in jobs.instances}

    name_width = max(len(j.get_job_short_name()) for j in jobs.instances)

    while not all(done.itervalues()):
        sys.stdout.write("\u001b[1000D")  # Move left
        sys.stdout.write("\u001b[{}A".format(count_lines()))  # Move up

        write_line(
            "Monitoring jobs in branch \u001b[33m{}\u001b[0m and matrix \u001b[33m{}\u001b[0m{}".format(
                jobs.branch, pretty_configurations(jobs.configurations), '.' * (feedback + 1)))

        feedback = (feedback + 1) % 3
        for job in jobs.instances:
            pretty_short_name = job.get_job_short_name().ljust(name_width)
            status = job.status
            duration = job.duration
            if duration is not None:
                pretty_duration = '{: 3d}:{:02d}'.format(int(duration // 60), int(math.floor(duration % 60)))
                if not job.done:
                    job.duration += PRINT_INTERVAL
            else:
                pretty_duration = '?'

            if status == STATUS_UNKNOWN:
                write_line("{}: [{}] {} / {}".format(pretty_short_name, "unknown".center(progress_width, " "), "?", pretty_duration))
            elif status == STATUS_BUILDING:
                progress = job.progress
                width = int(math.ceil(progress * progress_width))
                write_line("{}: [{}{}] {} / {}".format(pretty_short_name, "#" * width, " " * (progress_width - width), "{: 3d}%".format(int(round(progress * 100))), pretty_duration))
            elif status == STATUS_SUCCESS:
                write_line("{}: [{}] {} / {}".format(pretty_short_name, "#" * progress_width, "\u001b[32m{}\u001b[0m".format('success'), pretty_duration))
                done[job] = True
            elif status == STATUS_ABORT:
                progress = job.progress
                width = int(math.ceil(progress * progress_width))
                write_line("{}: [{}{}] {} / {}".format(pretty_short_name, "#" * width, " " * (progress_width - width), "\u001b[33m{}\u001b[0m".format('aborted'), pretty_duration))
                done[job] = True
            elif status in (STATUS_FAILURE, STATUS_INTERNAL_ERROR, STATUS_CONNECTION_ERROR):
                progress = job.progress
                width = int(math.ceil(progress * progress_width))
                caption = {
                    STATUS_FAILURE: 'failed',
                    STATUS_INTERNAL_ERROR: 'internal error',
                    STATUS_CONNECTION_ERROR: 'connection error',
                }[status]
                write_line("{}: [{}{}] {} / {}".format(pretty_short_name, "#" * width, " " * (progress_width - width), "\u001b[31m{}\u001b[0m".format(caption), pretty_duration))
                done[job] = True

                if status == STATUS_FAILURE:
                    failed.add(job)
            else:
                assert False, "unknown status"

        # Print URL to failed builds so people can just click on them and
        # see what happened
        for job in sorted(failed, key=lambda i: jobs.instances.index(i)):
            write_line("Go to {} for failed build in configuration \u001b[33m{}\u001b[0m".format(
                job.url, job.get_job_short_name()))

        yield trollius.sleep(PRINT_INTERVAL)


def read_jobs_done_matrix(filename):
    with open(filename, 'r') as f:
        yml = yaml.safe_load(f)
        matrix = yml.get('matrix')
        return matrix


def iter_jobs_combinations(matrix):
    import itertools
    names = matrix.keys()
    # full jobs_done values accept modifiers *after* a comma, which for steve isn't relevant
    splitted = [
        tuple(str(u).split(','))[0] for value in itertools.product(*matrix.values()) for u in value]

    for i in range(0, len(splitted), len(names)):
        yield {
            k: splitted[i + j]
            for j, k in enumerate(names)
        }


def parse_matrix_from_args(matrix_arg, full_matrix):
    matrix = {}
    for option in matrix_arg:
        key, values = option.split(':')
        key = key.strip()
        values = values.strip()

        if key not in full_matrix:
            raise ValueError(
                "Key {} in matrix argument is not recognized by project's jobs done configuration, "
                "known keys are {}".format(
                    "'{}'".format(key),
                    ",".join("'{}'".format(i) for i in full_matrix.keys()),
                ))

        matrix[key] = values.split(',')

    return matrix


def parse_parameters_from_args(parameters):
    parameters = [p.split(':') for p in parameters]
    parameters = {p[0]: p[1] for p in parameters}
    return parameters


def fill_missing_in_matrix(matrix, default_configuration):
    for key, value in default_configuration.items():
        if key not in matrix:
            matrix[key] = [value]

    return matrix


def get_platforms_from_comfigurations(matrix):
    return [i['platform'] for i in matrix]


def pretty_configurations(combinations):
    matrix = {}
    for combination in combinations:
        for k, v in combination.items():
            matrix.setdefault(k, set()).add(v)

    def as_strings():
        for key, values in matrix.items():
            yield "{}: {}".format(key, ",".join(values))

    return "; ".join(as_strings())


def read_config():
    from ConfigParser import ConfigParser, NoOptionError

    user_cfg = os.path.join(os.path.expanduser('~'), '.steve')
    configuration = Configuration()
    if os.path.isfile(user_cfg):
        config_parser = ConfigParser()
        config_parser.read(user_cfg)
        try:
            configuration.user = config_parser.get('user', 'name')
        except NoOptionError:
            pass
        try:
            configuration.password = config_parser.get('user', 'password')
        except NoOptionError:
            pass

    return configuration


def get_terminal_width():
    width = subprocess.check_output('tput cols', shell=True)
    width = int(width.strip())
    return width - 1


class Configuration:
    """
    :ivar unicode user: Jenkins username
    :ivar unicode password: Password or Jenkins API token (
        https://wiki.jenkins-ci.org/display/JENKINS/Authenticating+scripted+clients)
    """

    def __init__(self):
        self.user = None
        self.password = None
