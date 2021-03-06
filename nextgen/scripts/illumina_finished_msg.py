"""Script to check for finalized illumina runs and report to messaging server.

This is meant to be run via a cron job on a regular basis, and looks for newly
dumped output directories that are finished and need to be processed.

Usage:
    illumina_finished_msg.py <Galaxy config> <YAML local config>

The Galaxy config needs to have information on the messaging server and queues.
The local config should have the following information:

    msg_process_tag, msg_store_tag: tag names to send messages for processing and
                                    storage
    dump_directories: directories to check for machine output
    msg_db: flat file of output directories that have been reported
"""
import os
import sys
import json
import operator
import ConfigParser
import socket
import glob
import getpass
import subprocess

import yaml
from amqplib import client_0_8 as amqp

from bcbio.picard import utils
from bcbio.solexa.flowcell import (get_flowcell_info, get_fastq_dir)

def main(galaxy_config, local_config):
    amqp_config = _read_amqp_config(galaxy_config)
    with open(local_config) as in_handle:
        config = yaml.load(in_handle)
    search_for_new(config, amqp_config)

def search_for_new(config, amqp_config):
    """Search for any new directories that have not been reported.
    """
    reported = _read_reported(config["msg_db"])
    for dname in _get_directories(config):
        if os.path.isdir(dname) and dname not in reported:
            if _is_finished_dumping(dname):
                _update_reported(config["msg_db"], dname)
                _generate_fastq(dname)
                store_files, process_files = _files_to_copy(dname)
                finished_message(config["msg_process_tag"], dname,
                        process_files, amqp_config)
                finished_message(config["msg_store_tag"], dname,
                        store_files, amqp_config)

def _generate_fastq(fc_dir):
    """Generate fastq files for the current flowcell.
    """
    fc_name, fc_date = get_flowcell_info(fc_dir)
    short_fc_name = "%s_%s" % (fc_date, fc_name)
    fastq_dir = get_fastq_dir(fc_dir)
    if not fastq_dir == fc_dir and not os.path.exists(fastq_dir):
        with utils.chdir(os.path.split(fastq_dir)[0]):
            lanes = sorted(list(set([f.split("_")[1] for f in
                glob.glob("*qseq.txt")])))
            cl = ["solexa_qseq_to_fastq.py", short_fc_name,
                    ",".join(lanes)]
            subprocess.check_call(cl)
    return fastq_dir

def _is_finished_dumping(directory):
    """Determine if the sequencing directory has all files.

    The final checkpoint file will differ depending if we are a
    single or paired end run.
    """
    to_check = ["Basecalling_Netcopy_complete_SINGLEREAD.txt",
                "Basecalling_Netcopy_complete_READ2.txt"]
    return reduce(operator.or_,
            [os.path.exists(os.path.join(directory, f)) for f in to_check])

def _files_to_copy(directory):
    """Retrieve files that should be remotely copied.
    """
    with utils.chdir(directory):
        image_redo_files = reduce(operator.add,
		[glob.glob("*.params"),
		 glob.glob("Images/L*/C*"),
		 ["RunInfo.xml"]])
        qseqs = reduce(operator.add,
                     [glob.glob("Data/Intensities/*.xml"),
                      glob.glob("Data/Intensities/BaseCalls/*qseq.txt"),
                      ])
        reports = reduce(operator.add,
                     [glob.glob("Data/Intensities/BaseCalls/*.xml"),
                      glob.glob("Data/Intensities/BaseCalls/*.xsl"),
                      glob.glob("Data/Intensities/BaseCalls/*.htm"),
                      ["Data/Intensities/BaseCalls/Plots", "Data/reports"]])
        fastq = ["Data/Intensities/BaseCalls/fastq"]
    return sorted(image_redo_files + qseqs), sorted(reports + fastq)

def _read_reported(msg_db):
    """Retrieve a list of directories previous reported.
    """
    reported = []
    if os.path.exists(msg_db):
        with open(msg_db) as in_handle:
            for line in in_handle:
                reported.append(line.strip())
    return reported

def _get_directories(config):
    for directory in config["dump_directories"]:
        for dname in filter(lambda x: x.endswith("AAXX"),
                sorted(os.listdir(directory))):
            dname = os.path.join(directory, dname)
            if os.path.isdir(dname):
                yield dname

def _update_reported(msg_db, new_dname):
    """Add a new directory to the database of reported messages.
    """
    with open(msg_db, "a") as out_handle:
        out_handle.write("%s\n" % new_dname)

def finished_message(tag_name, directory, files_to_copy, config):
    """Wait for messages with the give tag, passing on to the supplied handler.
    """
    user = getpass.getuser()
    hostname = socket.gethostbyaddr(socket.gethostname())[0]
    data = dict(
            machine_type='illumina',
            hostname=hostname,
            user=user,
            directory=directory,
            to_copy=files_to_copy
            )
    conn = amqp.Connection(host=config['host'] + ":" + config['port'],
                           userid=config['userid'], password=config['password'],
                           virtual_host=config['virtual_host'], insist=False)
    chan = conn.channel()
    msg = amqp.Message(json.dumps(data),
                       content_type='application/json',
                       application_headers={'msg_type': tag_name})
    msg.properties["delivery_mode"] = 2
    chan.basic_publish(msg, exchange=config['exchange'],
                       routing_key=config['routing_key'])
    chan.close()
    conn.close()

def _read_amqp_config(galaxy_config):
    """Read connection information on the RabbitMQ server from Galaxy config.
    """
    config = ConfigParser.ConfigParser()
    config.read(galaxy_config)
    amqp_config = {}
    for option in config.options("galaxy_amqp"):
        amqp_config[option] = config.get("galaxy_amqp", option)
    return amqp_config

if __name__ == "__main__":
    main(*sys.argv[1:])
