0.5.1
=====

* Use of arbitrary terminal width caused undesired new lines to be printed
* Watcher coroutine was bugged when a request failed
* Updated readme

0.5.0
=====

* Huge refactoring to properly use jobs_done protocol
* Due to jobs_done refactoring, argument `-m/--mode` renamed `-m/--matrix` and had its goal changed too 
(see command help for details how to use new argument). Note that projects that have mode in matrix can obtain same
effect as former version by using `-m mode:MODE`
* Due to jobs_done refactoring, argument `-p/--platform` no longer exists, as it is now part of `-m/--matrix`
configuration. For instance, one can use `-m platform:win64` to force a build on Windows x64 platform
* Some improvements when jobs are cancelled to end more gracefully

0.4.0
=====

* CTRL+C prompts user if he wants to cancel builds

0.3.0
=====

* Can read user and password from user-wide configuration file

0.2.0
=====

* Decreased interval between requests to monitor jobs
* Prints URL of failed builds

0.1.0
=====

* Can guess jobs to run from user environment
* Can start a build from command-line
* Can run many platforms at once