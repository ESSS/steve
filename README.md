steve
=================

`steve` is a command-line tool tailored to how ESSS projects deal with
Jenkins. From any git repo containing an ESSS project it should be
possible to run a command line like:

```bash
steve -u foo
```

`steve` will use your current platform and branch (and most relevant 
mode, if your project uses that `jobs_done` feature) to guess job name 
in Jenkins and it will start right away to watch progress of current 
build of job (if none is in progress, it starts a new build).

Configuration file
------------------

`steve` looks for a configuration file named `.steve` in home of user
 (`~` on linux, `c:\users\<user_name>` on windows). The configuration
 file is an INI-like file, with following (all optional) fields 
 available so far:
  
```
[user]
name = <Jenkins user name>
password = <Jenkins password or, safer option, Jenkins API token>
```

Note that command line `-u/--user` has more priority than user from
configuration file.

For more information about [Jenkins API token]((https://wiki.jenkins-ci.org/display/JENKINS/Authenticating+scripted+clients).

## FAQ

*Will `steve` create new builds if one is already in progress?*

No, it just starts a new build if there isn't one already in progress.
It is possible to interrupt `steve` and restart it again to keep
monitoring progress of a same job.

*Do I have to always use current branch and platform?*

No. Use `-b/--branch BRANCH` to run other branch and 
`-m/--matrix platform:PLATFORMS` for other platforms. Notice that it is
possible to monitor many platforms at once, by using space separated
values (accepted values are the usual ones: `win64`, `win32`, 
`linux64`, `win64d`, `win32d`, `win64g`, `win32g`)

```bash
steve -u foo -m platform:win64,linux64
```

*Who is the most relevant mode? Can I change it too?*

Most relevant mode is the first value of each key in `matrix` of
`.jobs_done.yaml` file, except for platform that defaults to user's current
platform. 

To select other configuration `-m/--matrix MATRIX`. Matrix option must be
a series of keys separated by semicolons (`;`), where each key is followed by a
colon (`:`) and a series of values separated by comma (`,`).
 
To run a job both in Python 2.7 and 3.5:

```bash
steve -u foo -m python:27,35
```

*What about parametrized builds?*

You can pass any number of parameters to a build (provided the job declares
parameters):

```bash
steve -u foo -p param1:foo param2:bar
```

Note all other options absent in custom matrix are set to most relevant values,
as explained in *Who is the most relevant mode? Can I change it too?*.
