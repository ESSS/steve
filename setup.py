from __future__ import absolute_import, division, print_function, \
    unicode_literals

from setuptools import setup

version_locals = {}
with open('steve/version.py') as f:
    exec(f.read(), None, version_locals)
version = version_locals['__version__']

setup(
    name='steve',
    version=version,
    description='A tool that simplifies running Jenkins jobs from command-line',
    author='Fogo',
    author_email='bertoldi@esss.com.br',
    url='https://eden.esss.com.br/stash/users/bertoldi/repos/steve/browse',
    packages=['steve'],
    entry_points={
        'console_scripts': [
            'steve = steve.__main__:main',
        ],
    },
    install_requires=[
        'requests',
        'trollius',
        'pyyaml',
    ],
)
