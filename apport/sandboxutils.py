'''Functions to manage sandboxes'''

# Copyright (C) 2006 - 2009 Canonical Ltd.
# Author: Martin Pitt <martin.pitt@ubuntu.com>
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.  See http://www.gnu.org/copyleft/gpl.html for
# the full text of the license.

import atexit, os, os.path, shutil, sys, tempfile
import apport


def needed_packages(report):
    '''Determine necessary packages for given report.

    Return list of (pkgname, version) pairs. version might be None for unknown
    package versions.
    '''
    pkgs = {}

    # first, grab the versions that we captured at crash time
    for l in (report['Package'] + '\n' + report.get('Dependencies', '')).splitlines():
        if not l.strip():
            continue
        try:
            (pkg, version) = l.split()[:2]
        except ValueError:
            apport.warning('invalid Package/Dependencies line: %s', l)
            # invalid line, ignore
            continue
        pkgs[pkg] = version

    return [(p, v) for (p, v) in pkgs.items()]


def needed_runtime_packages(report, sandbox, cache_dir, verbose=False):
    '''Determine necessary runtime packages for given report.

    This determines libraries which were dynamically loaded at runtime, i. e.
    appear in /proc/pid/maps, but not in Dependencies: (such as plugins).

    Return list of (pkgname, None) pairs.

    When cache_dir is specified, it is used as a cache for get_file_package().
    '''
    # check list of libraries that the crashed process referenced at
    # runtime and warn about those which are not available
    pkgs = set()
    libs = set()
    if 'ProcMaps' in report:
        for l in report['ProcMaps'].splitlines():
            if not l.strip():
                continue
            cols = l.split()
            if len(cols) == 6 and 'x' in cols[1] and '.so' in cols[5]:
                lib = os.path.realpath(cols[5])
                libs.add(lib)

    if sandbox:
        cache_dir = os.path.join(cache_dir, report['DistroRelease'])

    # grab as much as we can
    for l in libs:
        if os.path.exists(sandbox + l):
            continue

        pkg = apport.packaging.get_file_package(l, True, cache_dir,
                                                arch=report.get('Architecture'))
        if pkg:
            if verbose:
                apport.log('dynamically loaded %s needs package %s, queueing' % (l, pkg))
            pkgs.add(pkg)
        else:
                apport.warning('%s is needed, but cannot be mapped to a package', l)

    return [(p, None) for p in pkgs]


def make_sandbox(report, config_dir, cache_dir=None, sandbox_dir=None,
                 extra_packages=[], verbose=False, log_timestamps=False):
    '''Build a sandbox with the packages that belong to a particular report.

    This downloads and unpacks all packages from the report's Package and
    Dependencies fields, plus all packages that ship the files from ProcMaps
    (often, runtime plugins do not appear in Dependencies), plus optionally
    some extra ones, for the distro release and architecture of the report.

    report is an apport.Report object to build a sandbox for. It needs to
    have at least a Package field, and usually also Dependencies, Architecture,
    and Uname.

    config_dir points to a directory with by-release configuration files for
    the packaging system, or "system"; this is passed to
    apport.packaging.install_packages(), see that method for details.

    cache_dir points to a directory where the downloaded packages and debug
    symbols are kept, which is useful if you create sandboxes very often. If
    not given, the downloaded packages get deleted at program exit.

    sandbox_dir points to a directory with a permanently unpacked sandbox with
    the already unpacked packages. This speeds up operations even further if
    you need to create sandboxes for different reports very often; but the
    sandboxes can become very big over time, and you must ensure that an
    already existing sandbox matches the DistroRelease: and Architecture: of
    report. If not given, a temporary directory will be created which gets
    deleted at program exit.

    extra_packages can specify a list of additional packages to install which
    are not derived from the report.

    If verbose is True (False by default), this will write some additional
    logging to stdout. If log_timestamps is True, these log messages will be
    prefixed with the current time.

    Return a tuple (sandbox_dir, cache_dir, outdated_msg).
    '''
    if sandbox_dir:
        sandbox_dir = os.path.abspath(sandbox_dir)
        if not os.path.isdir(sandbox_dir):
            os.makedirs(sandbox_dir)
        permanent_rootdir = True
    else:
        sandbox_dir = tempfile.mkdtemp(prefix='apport_sandbox_')
        atexit.register(shutil.rmtree, sandbox_dir)
        permanent_rootdir = False

    pkgs = needed_packages(report)
    for p in extra_packages:
        pkgs.append((p, None))
    if config_dir == 'system':
        config_dir = None

    # we call install_packages() multiple times, plus get_file_package(); use
    # a shared cache dir for these
    if cache_dir:
        cache_dir = os.path.abspath(cache_dir)
    else:
        cache_dir = tempfile.mkdtemp(prefix='apport_cache_')
        atexit.register(shutil.rmtree, cache_dir)

    try:
        outdated_msg = apport.packaging.install_packages(
            sandbox_dir, config_dir, report['DistroRelease'], pkgs,
            verbose, cache_dir, permanent_rootdir,
            architecture=report.get('Architecture'))
    except SystemError as e:
        sys.stderr.write(str(e) + '\n')
        sys.exit(1)

    pkgs = needed_runtime_packages(report, sandbox_dir, cache_dir, verbose)

    # package hooks might reassign Package:, check that we have the originally
    # crashing binary
    for path in ('InterpreterPath', 'ExecutablePath'):
        if path in report and not os.path.exists(sandbox_dir + report[path]):
            pkg = apport.packaging.get_file_package(report[path], True, cache_dir,
                                                    arch=report.get('Architecture'))
            if pkg:
                apport.log('Installing extra package %s to get %s' % (pkg, path), log_timestamps)
                pkgs.append((pkg, None))
            else:
                apport.warning('Cannot find package which ships %s', path)

    if pkgs:
        try:
            outdated_msg += apport.packaging.install_packages(
                sandbox_dir, config_dir, report['DistroRelease'], pkgs,
                cache_dir, architecture=report.get('Architecture'))
        except SystemError as e:
            sys.stderr.write(str(e) + '\n')
            sys.exit(1)

    for path in ('InterpreterPath', 'ExecutablePath'):
        if path in report and not os.path.exists(sandbox_dir + report[path]):
            apport.error('%s %s does not exist (report specified package %s)',
                         path, sandbox_dir + report[path], report['Package'])
            sys.exit(0)

    if outdated_msg:
        report['RetraceOutdatedPackages'] = outdated_msg

    apport.memdbg('built sandbox')

    return sandbox_dir, cache_dir, outdated_msg