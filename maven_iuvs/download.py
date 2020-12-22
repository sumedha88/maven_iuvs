import os
import glob
import subprocess
import time
import tempfile
import datetime
from getpass import getpass

# twill's __init__.py is dumb, we need to work around it to play nice
# with jupyter:
import sys
_stdout = sys.stdout
_stderr = sys.stdout

import twill
twill.set_output(_stdout)
twill.set_errout(_stderr)
twill.set_loglevel(twill.loglevels['WARNING'])


import pexpect
import paramiko

import numpy as np

from maven_iuvs.miscellaneous import clear_line
from maven_iuvs.search import get_latest_files


def get_user_paths_filename():
    """
    Determines whether user_paths.py exists and returns the filename
    if it does.

    Parameters
    ----------
    none

    Returns
    -------
    file_exists : bool
        Whether user_paths.py exists

    user_paths_py : str
       Absolute file path to user_paths.py
    """

    pyuvs_path = os.path.dirname(os.path.realpath(__file__))
    user_paths_py = os.path.join(pyuvs_path, "user_paths.py")

    file_exists = os.path.exists(user_paths_py)

    return file_exists, user_paths_py


def setup_user_paths():
    """
    Generates user_paths.py, used by sync_data to read data from the
    IUVS VM and store it locally

    Parameters
    ----------
    none

    Returns
    -------
    none

    Notes
    -------

    This is an interactive routine called once, generally the first
    time the user calls sync_data.

    """

    # if user_paths.py already exists then assume that the user has
    # set everything up already
    file_exists, user_paths_py = get_user_paths_filename()
    if file_exists:
        return

    # get the location of the default L1B and SPICE directory
    print("Syncing all of the L1B data could take up to 2TB of disk space.")
    l1b_dir = input("Where would you like IUVS l1b FITS files"
                    " to be stored by sync_data? ")
    print("Syncing all of the SPICE kernels could take up to 300GB of disk"
          " space.")
    spice_dir = input("Where would you like MAVEN/IUVS SPICE"
                      " kernels to be stored by sync_data? ")
    # get the VM username to be used in rsync calls
    vm_username = input("What is your username for the"
                        " IUVS VM to sync files? ")

    user_paths_file = open(user_paths_py, "x")

    user_paths_file.write("# This file automatically generated by"
                          " maven_iuvs.download.setup_file_paths\n")
    user_paths_file.write("l1b_dir = \""+l1b_dir+"\"\n")
    user_paths_file.write("spice_dir = \""+spice_dir+"\"\n")
    user_paths_file.write("iuvs_vm_username = \""+vm_username+"\"\n")

    user_paths_file.close()
    # now scripts can import the relevant directories from user_paths


def call_rsync(remote_path,
               local_path,
               ssh_password,
               extra_flags=""):
    """
    Updates the SPICE kernels by rsyncing the VM folders to the local machine.

    Parameters
    ----------
    remote_path : str
        Path to sync on the remote machine.

    local_path : str
        Path to the sync on the local machine.

    ssh_password : str
        Plain text to send to process when it prompts for a password

    extra_flags : str
        Extra flags for rsync command.

        -trzL and -info=progress2 are already specified, extra_flags
         text are inserted afterward. Defaults to "".

    Returns
    -------
    none

    """
    # get the version number of rsync
    try:
        result = subprocess.run(['rsync', '--version'],
                                stdout=subprocess.PIPE,
                                check=True)
        version = result.stdout.split(b'version')[1].split()[0]
        version = int(version.replace(b".", b""))
    except subprocess.CalledProcessError:
        raise Exception("rsync failed ---"
                        " is rsync installed on your system?")

    if version >= 313:
        # we can print total transfer progress
        progress_flag = '--info=progress2'
    else:
        progress_flag = '--progress'

    rsync_command = " ".join(['rsync -trvzL',
                              progress_flag,
                              extra_flags,
                              remote_path,
                              local_path])

    print("running rsync_command: " + rsync_command)
    child = pexpect.spawn(rsync_command,
                          encoding='utf-8')

    cpl = child.compile_pattern_list(['.* password: ',
                                      '[0-9]+%'])
    child.expect_list(cpl)

    if 'password' in child.after:
        # respond to server password request
        child.sendline(ssh_password)

    # print some progress info by searching for lines with a
    # percentage progress
    cpl = child.compile_pattern_list([pexpect.EOF,
                                      '[0-9]+%'])
    while True:
        i = child.expect_list(cpl, timeout=None)
        if i == 0:  # end of file
            break
        if i == 1:
            percent = child.after.strip(" \t\n\t")

            # get file left to check also
            child.expect('[0-9]+/[0-9]+', timeout=None)
            file_numbers = child.after

            if version < 313:
                # compute progress from file numbers
                fnum1, fnum2 = list(map(int, file_numbers.split("/")))
                percent = 1.0 - fnum1 / fnum2
                percent = str(int(percent*100)) + "%"

            clear_line()
            print("rsync progress: " +
                  percent +
                  ' (files left: ' + file_numbers + ')',
                  end='\r')

    child.close()
    clear_line()  # clear last rsync message


def get_vm_file_list(server,
                     serverdir,
                     username,
                     password,
                     pattern="*.fits*",
                     minorb=100, maxorb=100000,
                     include_cruise=False,
                     status_tag=""):
    """
    Get a list of files from the VM that match a given pattern.

    Parameters
    ----------
    server : str
        name of the server to get files from (normally maven-iuvs-itf)

    serverdir : str
        directory to search for files matching the pattern

    username : str
        username for server access

    password : str
        password for server access

    pattern : str
        glob pattern used to search for matching files
        Defaults to '*.fits*' (matches all FITS files)

    minorb, maxorb : int
        Minimum and maximum orbit numbers to sync from VM, in multiples of 100.
        Defaults to 100 and 100000, but smaller ranges than the available data
        will sync faster.

    include_cruise : bool
        Whether to sync cruise data in addition to the orbit range above.
        Defaults to False.

    status_tag : str
        Tag to decorate orbit number print string and inform user of progress.
        Defaults to "".

    Returns
    -------
    files : np.array
        list of server filenames that match the pattern
    """

    # connect to the server using paramiko
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.connect(server, username=username, password=password)

    # get the list of folders on the VM
    stdin, stdout, stderr = ssh.exec_command('ls '+serverdir)
    server_orbit_folders = np.loadtxt(stdout, dtype=str)

    # determine what folders to look for files in
    sync_orbit_folders = ["orbit"+str(orbno).zfill(5)
                          for orbno in np.arange(minorb, maxorb, 100)]
    if include_cruise:
        sync_orbit_folders = np.append(["cruise"], sync_orbit_folders)

    # sync only folders that belong to both groups
    sync_orbit_folders = server_orbit_folders[np.isin(server_orbit_folders,
                                                      sync_orbit_folders,
                                                      assume_unique=True)]

    # set up the output files array
    files = []

    # iterate through the folder list and get the filenames that match
    # the input pattern
    for folder in sync_orbit_folders:
        clear_line()
        print(status_tag+folder, end="\r")

        cmd = "ls "+serverdir+folder+"/"+pattern
        stdin, stdout, stderr = ssh.exec_command(cmd)

        if len(stderr.read()) == 0:
            files.append(np.loadtxt(stdout, dtype=str))
        else:
            continue
    ssh.close()

    if len(files) == 0:
        return []
    else:
        return np.concatenate(np.array(files, dtype=object))


def sync_data(spice=True, l1b=True,
              pattern="*.fits*",
              minorb=100, maxorb=100000,
              include_cruise=False):
    """
    Synchronize new SPICE kernels and L1B data from the VM and remove
    any old files that have been replaced by newer versions.

    Parameters
    ----------
    spice : bool
        Whether or not to sync SPICE kernels. Defaults to True.

    l1b : bool
        Whether or not to sync level 1B data. Defaults to True.

    pattern : str
        glob pattern used to search for matching files

        Defaults to '*.fits*' (matches all FITS files)

    minorb, maxorb : int
        Minimum and maximum orbit numbers to sync from VM, in multiples of 100.

        Defaults to 100 and 100000, but smaller ranges than the available data
        will sync faster.

    include_cruise : bool
        Whether to sync cruise data in addition to the orbit range above.

        Defaults to False.

    Returns
    -------
    None.

    """

    #  check if user path data exists and set it if not
    setup_user_paths()
    #  load user path data from file
    from maven_iuvs.user_paths import l1b_dir, spice_dir, iuvs_vm_username
    if not os.path.exists(spice_dir):
        raise Exception("Cannot find specified SPICE directory."
                        " Is it accessible?")
    if not os.path.exists(l1b_dir):
        raise Exception("Cannot find specified L1B directory."
                        " Is it accessible?")

    # get starting time
    t0 = time.time()

    # define VM-related variables
    vm = 'maven-iuvs-itf'
    login = iuvs_vm_username + '@' + vm + ':'
    production_l1b = '/maven_iuvs/production/products/level1b/'
    stage_l1b = '/maven_iuvs/stage/products/level1b/'
    vm_spice = login + '/maven_iuvs/stage/anc/spice/'

    # try to sync the files, if it fails, user probably isn't on the VPN
    try:
        # get user password for the VM
        iuvs_vm_password = getpass('input password for '+login+' ')

        # sync SPICE kernels
        if spice is True:
            print('Updating SPICE kernels...')
            call_rsync(vm_spice, spice_dir, iuvs_vm_password,
                       extra_flags="--delete")

        # sync level 1B data
        if l1b is True:
            # get the file names of all the relevant files
            print('Fetching names of level 1B production and stage'
                  ' files from the VM...')
            prod_filenames = get_vm_file_list(vm,
                                              production_l1b,
                                              iuvs_vm_username,
                                              iuvs_vm_password,
                                              pattern=pattern,
                                              minorb=minorb,
                                              maxorb=maxorb,
                                              include_cruise=include_cruise,
                                              status_tag='production: ')
            stage_filenames = get_vm_file_list(vm,
                                               stage_l1b,
                                               iuvs_vm_username,
                                               iuvs_vm_password,
                                               pattern=pattern,
                                               minorb=minorb,
                                               maxorb=maxorb,
                                               include_cruise=include_cruise,
                                               status_tag='stage: ')
            local_filenames = glob.glob(l1b_dir+"/*/"+pattern)

            # get the list of most recent files, no matter where they are
            #    order matters! putting local_filenames first ensures
            #    duplicates aren't transferred
            if (len(prod_filenames) == 0 and len(stage_filenames) == 0):
                print("No matching files on VM")
                return

            files_to_sync = get_latest_files(np.concatenate([local_filenames,
                                                             prod_filenames,
                                                             stage_filenames]))

            # figure out which files to get from production and stage
            files_from_production = [a[len(production_l1b):]
                                     for a in files_to_sync
                                     if (a[:len(production_l1b)]
                                         ==
                                         production_l1b)]
            files_from_stage = [a[len(stage_l1b):]
                                for a in files_to_sync
                                if a[:len(stage_l1b)] == stage_l1b]

            # production
            # save the files to rsync to temporary files
            # this way rsync can use the files_from flag
            transfer_from_production_file = tempfile.NamedTemporaryFile()
            np.savetxt(transfer_from_production_file.name,
                       files_from_production,
                       fmt="%s")

            print('Syncing ' + str(len(files_from_production)) +
                  ' files from production...')
            call_rsync(login+production_l1b,
                       l1b_dir,
                       iuvs_vm_password,
                       extra_flags=('--files-from=' +
                                    transfer_from_production_file.name))

            # stage, identical to above
            transfer_from_stage_file = tempfile.NamedTemporaryFile()
            np.savetxt(transfer_from_stage_file.name,
                       files_from_stage,
                       fmt="%s")

            print('Syncing ' + str(len(files_from_stage)) +
                  ' files from stage...')
            call_rsync(login+stage_l1b,
                       l1b_dir,
                       iuvs_vm_password,
                       extra_flags=('--files-from=' +
                                    transfer_from_stage_file.name))

            # now delete all of the old files superseded by newer versions
            clear_line()
            print('Cleaning up old files...')

            # figure out what files need to be deleted
            local_filenames = glob.glob(l1b_dir+"/*/*.fits*")
            latest_local_files = get_latest_files(local_filenames)
            local_files_to_delete = np.setdiff1d(local_filenames,
                                                 latest_local_files)

            # ask if it's OK to delete the old files
            while True and len(local_files_to_delete) > 0:
                del_files = input('Delete ' +
                                  str(len(local_files_to_delete)) +
                                  ' old files? (y/n/p)')
                if del_files == 'n':
                    # don't delete the files
                    break
                if del_files == 'y':
                    # delete the files
                    [os.remove(f) for f in local_files_to_delete]
                    break
                if del_files == 'p':
                    print(local_files_to_delete)
                else:
                    print("Please answer y or n, or p to print the file list.")

            # Question for merge manager:
            # Kyle's code keeps a list of these deleted files
            # in excluded_files.txt --- is this necessary?

            # index all local files to speed up later finding
            local_filenames = sorted(glob.glob(l1b_dir+"/*/*.fits*"))
            np.save(l1b_dir+'/filenames', sorted(local_filenames))

    except OSError:
        raise Exception('rsync failed --- are you connected to the VPN?')

    # get ending time
    t1 = time.time()
    seconds = t1 - t0
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)

    # tell us how long it took
    print('Data syncing and cleanup took %.2d:%.2d:%.2d.' % (h, m, s))


def get_euvm_l2b_dir():
    """
    Returns the directory where euvm_l2b data should be stored.

    Parameters
    ----------
    none

    Returns
    -------
    euvm_l2b_dir : str
       Directory to store EUVM L2B files in.
    """

    pyuvs_path = os.path.dirname(os.path.realpath(__file__))
    user_paths_py = os.path.join(pyuvs_path, "user_paths.py")

    if not os.path.exists(user_paths_py):
        setup_user_paths()

    try:
        from maven_iuvs.user_paths import euvm_l2b_dir
    except ImportError:
        # need to set euvm_l2b_dir
        euvm_l2b_dir = input("Where should euvm_l2b data be stored?")
        with open(user_paths_py, "a+") as f:
            f.write("# This line added by get_euvm_l2b_dir.py\n")
            f.write("euvm_l2b_dir = '"+euvm_l2b_dir+"'\n")

    return euvm_l2b_dir


def sync_euvm_l2b(sdc_username, sdc_password):
    """
    Sync EUVM L2B data file from MAVEN SDC. This deletes all old data
    in euvm_l2b_dir and replaces it with a newly downloaded file.

    Parameters
    ----------
    sdc_username : str
        Web login username for MAVEN SDC Team site.
    sdc_password : str
        Web login password for MAVEN SDC Team site.

    Returns
    -------
    none

    """
    print("syncing EUVM L2B...")

    url = 'https://lasp.colorado.edu/maven/data/sci/euv/l2b/'

    euvm_l2b_dir = get_euvm_l2b_dir()

    # go to the SDC webpage and expect to see a login form
    twill.browser.reset()
    twill.browser.go(url)

    # enter the login info
    twill.commands.fv("1", 'username', sdc_username)
    twill.commands.fv("1", 'password', sdc_password)
    twill.browser.submit()

    # load the page now that we're authenticated
    twill.browser.go(url)

    # find the most recent save file on the page
    files = sorted([f.url for f in twill.browser.links if '.sav' in f.url])
    most_recent = files[-1]

    # navigate to that file
    twill.browser.go(url+most_recent)

    # delete old EUVM files in the EUVM l2b directory
    old_fnames = glob.glob(euvm_l2b_dir+'*l2b*.sav')
    [os.remove(f) for f in old_fnames]

    # save the new file to disk
    fname = euvm_l2b_dir + most_recent
    with open(fname, "wb") as file:
        file.write(twill.browser.dump)


def get_integrated_reports_dir():
    """
    Returns the directory where MAVEN integrated reports files should be
    stored.

    Parameters
    ----------
    none

    Returns
    -------
    integrated_reports_dir : str
        Directory to store MAVEN integrated reports in.

    """

    pyuvs_path = os.path.dirname(os.path.realpath(__file__))
    user_paths_py = os.path.join(pyuvs_path, "user_paths.py")

    if not os.path.exists(user_paths_py):
        setup_user_paths()

    try:
        from maven_iuvs.user_paths import integrated_reports_dir
    except ImportError:
        # need to set euvm_l2b_dir
        integrated_reports_dir = input("Where should MAVEN Integrated Reports"
                                       " data be stored?")
        with open(user_paths_py, "a+") as f:
            f.write("# This line added by get_integrated_reports_dir.py\n")
            f.write("integrated_reports_dir = '"+integrated_reports_dir+"'\n")

    return integrated_reports_dir


def sync_integrated_reports(sdc_username, sdc_password, check_old=False):
    """Sync Integrated Reports data from MAVEN Ops page. Syncs all new
    files and all files from last 180 days by default.

    Parameters
    ----------
    sdc_username : str
        Web login username for MAVEN SDC Team site.
    sdc_password : str
        Web login password for MAVEN SDC Team site.
    check_old : bool
        Whether to check all files in the integrated_reports_dir
        against the server. Defaults to False.

    Returns
    -------
    none

    """

    print("syncing Integrated Reports...")
    
    url = ('https://lasp.colorado.edu/ops/maven/team/'
           + 'inst_ops.php?content=msa_ir&show_all')

    local_ir_dir = get_integrated_reports_dir()

    # go to the SDC webpage and expect to see a login form
    twill.browser.reset()
    twill.browser.go(url)

    # enter the login info
    twill.commands.fv("1", 'username', sdc_username)
    twill.commands.fv("1", 'password', sdc_password)
    twill.browser.submit()

    # load the page now that we're authenticated
    twill.browser.go(url)

    # get the list of integrated report files on the server
    server_links = sorted([f for f in twill.browser.links if '.txt' in f.text])

    # get the list of local integrated report files
    local_files = [os.path.basename(f)
                   for f in glob.glob(os.path.join(local_ir_dir, '*'))]

    if check_old:
        # check all the files, not just the ones we don't have
        to_download = server_links
    else:
        # figure out which ones on the server are new
        old_time = datetime.datetime.now() - datetime.timedelta(days=180)
        old_time = old_time.strftime("%y%m%d")
        to_download = [f for f in server_links if ((int(f.text.split("_")[2])
                                                    > int(old_time))
                                                   or (f.text
                                                       not in local_files))]

    # download the new files
    from lxml.etree import ParserError
    for link in to_download:
        clear_line()
        print(link.text, end="\r")

        # modify the page link to a download link
        download_link = link.url.replace("inst_ops.php?content=file&file=",
                                         "download-file.php?public/")

        # get the binary of the file
        try:
            twill.browser.go(download_link)
            server_binary_data = twill.browser.dump
        except ParserError:
            # sometimes the files have zero size,
            # which results in a ParserError
            server_binary_data = b""

        # get the local filename
        fname = os.path.join(local_ir_dir, link.text)

        # look at the local file contents and compare with remote
        if os.path.exists(fname):
            with open(fname, "rb") as file:
                if file.read() == twill.browser.dump:
                    # file is the same as the server, keep it
                    continue

        # if we're here either the local file doesn't exist
        # or it's different from the server copy.
        # Either way, download the server version
        fname = os.path.join(local_ir_dir, link.text)
        with open(fname, "wb") as file:
            file.write(server_binary_data)

    clear_line()


def sync_sdc(check_old=False):
    """Wrapper routine to sync EUVM L2B data and Integrated Reports from
    MAVEN SDC.

    Parameters
    ----------
    check_old : bool
        Whether to check all files in the integrated_reports_dir
        against the server. Defaults to False.

    Returns
    -------
    none

    """

    username = input('Username for MAVEN Team SDC: ')
    password = getpass('password for '+username+' on MAVEN Team SDC: ')

    sync_euvm_l2b(username, password)
    sync_integrated_reports(username, password, check_old=check_old)