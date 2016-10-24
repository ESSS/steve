from __future__ import absolute_import, division, print_function, \
    unicode_literals

import argparse
import sys


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    from steve.steve import run, read_config
    from steve.version import __version__

    configuration = read_config()

    parser = argparse.ArgumentParser(
        prog='steve',
        description='A tool that simplifies running Jenkins jobs from command-line')
    parser.add_argument(
        '-u', '--user', default=configuration.user,
        help='Jenkins username, does not need to be provided if present in configuration file')
    parser.add_argument(
        '-b', '--branch', default=None, help='Branch of the CI job to trigger (defaults to '
                                             'current branch)')
    parser.add_argument(
        '-m', '--matrix', default=None, help='Desired jobs_done matrix configuration. Must be in'
                                             'a dict-like format (for example: '
                                             'python:35; platform:win64,linux64). This '
                                             'configuration prevails over default or configuration'
                                             'file values and it is used to define how many jobs'
                                             'are going to be executed.')
    parser.add_argument('--debug', action='store_true', help='Enables debugging')
    parser.add_argument(
        '-v', '--version',
        action='version', version='%(prog)s {}'.format(__version__))
    args = parser.parse_args(args=args)

    if not args.user:
        parser.error('unable to determine user, add user to ~/.steve or '
                     'argument -u/--user is required')
        return 1

    return run(
        user=args.user,
        branch=args.branch,
        matrix=args.matrix,
        configuration=configuration,
        debug=args.debug,
    )

if __name__ == '__main__':
    sys.exit(main())
