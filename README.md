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

## FAQ

*Will `steve` create new builds if one is already in progress?*

No, it just starts a new build if there isn't one already in progress.
It is possible to interrupt `steve` and restart it again to keep
monitoring progress of a same job.

*Do I have to always use current branch and platform?*

No. Use `-b/--branch BRANCH` to run other branch and 
`--p/--platform PLATFORMS` for other platforms. Notice that it is
possible to monitor many platforms at once, by using space separated
values (accepted values are the usual ones: `win64`, `win32`, 
`linux64`, `win64d`, `win32d`)

```bash
steve -u foo -p win64 linux64
```

*Who is the most relevant mode? Can I change it too?*

Most relevant mode is the first one in list of modes available in
`.jobs_done.yaml` file (note that if no mode is found `steve` should
work as well). To change mode use `-m/--mode MODE`.
