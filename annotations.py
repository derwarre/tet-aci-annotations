import os
import re
import logging
import threading
from csv import writer
from tempfile import NamedTemporaryFile
from threading import Thread
from collections import deque
from time import sleep, time
import tempfile
import argparse

import requests
from pylru import lrudecorator
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from tetpyclient import MultiPartOption, RestClient

import acitoolkit.acitoolkit as aci

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

@lrudecorator(200)
def get_tenant_deep(session, tenant):
    return aci.Tenant.get_deep(session, names=(tenant.name, ))[0]


@lrudecorator(1000)
def get_ctx_and_bd(session, tenant, app, epg):
    searcher = aci.Search()
    searcher.name = epg.name
    deep_tenant = get_tenant_deep(session, tenant)
    try:
        found_epg = deep_tenant.find(searcher)[0]
        try:
            bd = found_epg.get_bd()
        except AttributeError:
            raise ValueError("No BD")
        try:
            ctx = bd.get_context()
        except AttributeError:
            raise ValueError("No Context")
        try:
            return bd.name, ctx.name
        except AttributeError:
            raise ValueError("No Context")
    except (IndexError, ValueError):
        return "None", "None"


class StoppableThread(Thread):
    """Thread class with a stop() method. The thread itself has to check
    regularly for the stopped() condition."""

    def __init__(self):
        super(StoppableThread, self).__init__()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()


class Track(StoppableThread):
    def __init__(self, config):
        super(Track, self).__init__()
        self.daemon = True
        self.config = config
        self.log = deque([], maxlen=10)
        self.annotations = {}
        self.lock = threading.Lock()

    def reset(self):
        self._stop_event = threading.Event()

    def run(self):
        th = Thread(target=self.upload_annotations)
        th.daemon = True
        th.start()
        self.th = th
        self.track()

    def upload_annotations(self):
        if 'creds' in self.config:
            restclient = RestClient(
                "https://" + self.config["url"],
                credentials_file=self.config['creds'],
                verify=self.config["verify"])
        else:
            restclient = RestClient(
                "https://" + self.config["url"],
                api_key=self.config["key"],
                api_secret=self.config["secret"],
                verify=self.config["verify"])
        # sleep for 30 seconds to stagger uploading
        sleep(30)

        #selected_annotations = sorted([
        #    annotation
        #    for annotation, include in self.config["annotations"].items()
        #    if include
        #])

        labels = {
            "mac": 'ACI MAC',
            "bd": 'ACI Bridge Domain',
            "vrf": 'ACI VRF',
            "tenant": 'ACI Tenant',
            "app": 'ACI Application Profile',
            "epg": 'ACI End Point Group',
            "intf": 'ACI Attached Interface',
            "ts": 'ACI Last Endpoint Move Time Stamp',
            "epg_dn": 'ACI EPG DN',
            "leaf": 'ACI Leaf'
        }
        headers = [labels[key] for key in self.config['annotations']]
        headers.insert(0, "IP")

        while True:
            if self.stopped():
                print "Cleaning up annotation thread"
                return
            if self.annotations:
                try:
                    # Acquire the lock so we don't have a sync issue
                    # if an endpoint receives an event while we upload
                    # data to Tetration
                    self.lock.acquire()
                    print "Writing Annotations (Total: %s) " % len(
                        self.annotations)
                    with NamedTemporaryFile() as tf:
                        wr = writer(tf)
                        wr.writerow(headers)
                        for att in self.annotations.values():
                            row = [att[key] for key in self.config['annotations']]
                            row.insert(0, att["ip"])
                            wr.writerow(row)
                        tf.seek(0)

                        req_payload = [
                            MultiPartOption(
                                key='X-Tetration-Oper', val='add')
                        ]
                        print '/openapi/v1/assets/cmdb/upload/{}'.format(self.config["vrf"])
                        resp = restclient.upload(
                            tf.name, '/openapi/v1/assets/cmdb/upload/{}'.format(
                                self.config["vrf"]), req_payload)
                        if resp.ok:
                            print "Uploaded Annotations"
                            self.log.append({
                                "timestamp": time(),
                                "message":
                                "{} annotations".format(len(self.annotations))
                            })
                            self.annotations.clear()
                        else:
                            print "Failed to Upload Annotations"
                            print resp.text
                finally:
                    self.lock.release()
            else:
                print "No new annotations to upload"
            print "Waiting {} seconds".format(int(self.config["frequency"]))
            sleep(int(self.config["frequency"]))

    def track(self):
        print "Collecting existing Endpoint data..."
        session = aci.Session(self.config['apic_url'], self.config['apic_user'], self.config['apic_pw'])
        resp = session.login()

        while True:
            print "Searching for endpoints"
            # Download all of the Endpoints
            endpoints = aci.Endpoint.get(session)
            for ep in endpoints:
                try:
                    epg = ep.get_parent()
                except AttributeError:
                    continue
                if ep.ip != "0.0.0.0":
                    app_profile = epg.get_parent()
                    tenant = app_profile.get_parent()
                    bd, vrf = get_ctx_and_bd(session, tenant, app_profile, epg)
                    if ep.if_dn:
                        for dn in ep.if_dn:
                            match = re.match('protpaths-(\d+)-(\d+)',
                                             dn.split('/')[2])
                            if match:
                                if match.group(1) and match.group(2):
                                    int_name = match.group(1) + "-" + match.group(2) + " " + ep.if_name
                                    leaf = match.group(1) + "-" + match.group(2)
                    else:
                        int_name = ep.if_name
                        leaf = ep.if_name.split('/')[1]
                    try:
                        data = {
                            "ip": ep.ip,
                            "mac": ep.mac,
                            "bd": bd,
                            "vrf": vrf,
                            "tenant": tenant.name,
                            "app": app_profile.name,
                            "epg": epg.name,
                            "intf": int_name,
                            "leaf": leaf,
                            "ts": ep.timestamp,
                            "epg_dn": "uni/tn-{}/ap-{}/epg-{}".format(
                                tenant.name, app_profile.name, epg.name)
                        }
                        self.lock.acquire()
                        self.annotations[ep.ip] = data
                        self.lock.release()
                    except ValueError:
                        continue
                else:
                    continue
            if self.stopped():
                print "Cleaning up track thread"
                return
            sleep(int(self.config["frequency"]))

def main():
    """
    Main execution routine
    """
    conf_vars = {
                'tet_url':{
                    'descr':'Tetration API URL',
                    'envs':['TA_ACI_URL'],
                    'conf':'url'
                    },
                'tet_creds_file':{
                    'descr':'Tetration API Credentials File',
                    'envs':['TA_ACI_API_KEY','TA_ACI_API_SECRET'],
                    'conf':'creds',
                    'alt-conf':['key','secret']
                    },
                'frequency':{
                    'descr':'Frequency to pull from APIC and upload to Tetration',
                    'default':300,
                    'conf':'frequency'
                    },
                'apic_url':{
                    'descr':'Tetration API Credentials File',
                    'envs':['TA_ACI_APIC_URL'],
                    'conf':'apic_url'
                    },
                'apic_user':{
                    'descr':'APIC URL',
                    'envs':['TA_ACI_APIC_USER'],
                    'conf':'apic_user'
                    },
                'apic_pw':{
                    'descr':'APIC Username',
                    'envs':['TA_ACI_APIC_PW'],
                    'conf':'apic_pw'
                    },
                'tenant':{
                    'descr':'Tetration Tenant Name',
                    'envs':['TA_ACI_TENANT'],
                    'conf':'vrf'
                    }
                }
    
    parser = argparse.ArgumentParser(description='Tetration-ACI Annotator')
    for item in conf_vars:
        descr = conf_vars[item]['descr']
        if 'envs' in conf_vars[item]:
            descr = '{} - Can alternatively be set via environment variable(s) {}'.format(conf_vars[item]['descr'],' and '.join(conf_vars[item]['envs']))
        default = None
        if 'default' in conf_vars[item]:
            default = conf_vars[item]['default']
        parser.add_argument('--'+item,default=default,help=descr)
    args = parser.parse_args()

    config = {'verify':False,'annotations':['bd','vrf','app','epg','intf','leaf']}
    errors = []
    for arg in vars(args):
        attribute = getattr(args, arg)
        if attribute == None:
            try:
                for i,item in enumerate(conf_vars[arg]['envs']):
                    if os.environ.get(item) == None:
                        errors.append(arg)
                    else:
                        if len(conf_vars[arg]['envs'])>0:
                            config[conf_vars[arg]['alt-conf'][i]] = os.environ.get(item)
                        else:
                            config[conf_vars[arg]['conf']] = os.environ.get(item)
            except:
                errors.append(arg)
                continue
        else:
            config[conf_vars[arg]['conf']] = attribute
    if len(errors) > 0:
        print('Required variables not provided for: \n  --{}\nRun with --help to see all required options'.format('\n  --'.join(errors)))
    else:
        tracker = Track(config)
        tracker.run()

if __name__ == '__main__':
    main()