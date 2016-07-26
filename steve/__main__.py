from __future__ import absolute_import, division, print_function, \
    unicode_literals

import sys


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    from steve.steve import run
    run(args)

if __name__ == '__main__':
    # Set it to enable debug feature of trollius
    # os.environ['TROLLIUSDEBUG'] = '1'

    # Uncomment and configure for logging
    # logger = logging.getLogger()
    # logger.setLevel(logging.DEBUG)
    # logger.addHandler(logging.FileHandler('steve.log'))
    sys.exit(main())
