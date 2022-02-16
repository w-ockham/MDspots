#!/usr/bin/env python3
# coding: utf-8
import datetime
from os import access
import pickle
import urllib.request
import json
import re
import sys
import sqlite3
import time
from twython import Twython

consumer = ''
consumer_sec = ''
access = ''
access_sec = ''
myid =  ''

class POTASpotter:
    def __init__(self, myid, interval=70, prefix = 'JA-'):
        self.myid = myid;
        self.interval = interval
        self.prefix = prefix
        self.potaapi = 'https://api.pota.app/spot/activator/'
        self.logdir = '/var/log/potaspots/'
        self.homedir = '/home/ubuntu/sotaapp/backend/'
        self.logfile = 'potaspot.log'
        self.api = Twython(consumer, consumer_sec, access, access_sec)
        self.db = sqlite3.connect(self.homedir + 'potaspot.db')
        self.cur = self.db.cursor()

        try:
            with open(self.homedir + 'lastid.pkl', mode='rb') as f:
                saved = pickle.load(f)
                self.lastid = saved['lastid']
                self.lastmsg = saved['lastmsg']
        except Exception as e:
            self.lastid = 0
            self.lastmsg = 0
            q = 'create table if not exists potaspots(utc int, time text, callsign text, freq real, mode text, ref text, park text, comment text, primary key(callsign))'
            self.cur.execute(q)


    def log(self, mesg):
        with open(self.logdir + self.logfile, mode='a') as f:
            print(mesg, file=f)

    def interp(self, cmd):
        self.now = int(datetime.datetime.utcnow().strftime("%s"))
        command = cmd.upper().split()
        if 'JA' in command[0]:
            ref = 'JA-%'
            freq = -1
        elif 'DX' in command[0]:
            ref = '%'
            freq = 100000
        else:
            ref = 'JA-%'
            freq = -1

        lastseen = self.now - 3600
        if len(command) > 1:
            try:
                lastseen = self.now - 3600 * int(command[1])
            except ValueError:
                lastseen = self.now - 3600

        if freq > 0:
            q = f"select * from potaspots where utc > {lastseen} and freq <= {freq} and ref like '{ref}'"
        else:
            q = f"select * from potaspots where utc > {lastseen} and ref like '{ref}'"

        mesg = ''
        self.cur.execute(q)
        for s in self.cur.fetchall():
            (_, tm, call, freq, mode, ref, park, comment) = s
            mesg += f"{tm} {call} {ref} {freq} {mode} {comment}\n"

        if mesg == '':
            mesg = 'No Spots.'

        return mesg

    def check_dm(self):
        res = self.api.get_direct_messages()
        msglist = [ m for m in res["events"] if int(m["created_timestamp"]) > self.lastmsg ]
        if msglist:
            self.lastmsg = max(int(i['created_timestamp']) for i in msglist)

        msglist.sort(key=(lambda x: x['created_timestamp']))

        for usr in msglist:
            rcpt = usr["message_create"]["sender_id"]
            if rcpt != self.myid:
                cmd = usr["message_create"]["message_data"]["text"]
                msgevent = {
                    "type": "message_create",
                    "message_create": { "target":{"recipient_id": rcpt },
                                        "message_data":
                                        {"text": self.interp(cmd)}}}
                self.api.send_direct_message(event = msgevent)
                self.log(msgevent)

    def run(self):
        while True:
            self.now = int(datetime.datetime.utcnow().strftime("%s"))

            try:
                spotobj = urllib.request.urlopen(self.potaapi)
                spotdata = json.loads(spotobj.read())
            except Exception as e:
                self.log(f'Error: {e}')
                time.sleep(self.interval)
                continue

            if spotdata:
                spots =[s for s in spotdata[::-1]
                        if int(s['spotId']) > self.lastid]
                for s in spots:
                    sid = s['spotId']
                    ref = s['reference']
                    activator = s['activator']
                    freq = s['frequency']
                    mode = s['mode']
                    park = s['name']
                    loc  = s['locationDesc']
                    spotter = s['spotter']
                    comment = s['comments']
                    lat = s['latitude']
                    lon = s['longitude']
                    hhmm= datetime.datetime.fromisoformat(s['spotTime']).strftime('%H:%M')
                    mesg = f'{hhmm} {activator} on {ref}({loc} {park}) {freq} {mode} {comment}[{spotter}]'

                    m = re.match(self.prefix, ref)
                    if m:
                        try:
                            self.api.update_status(status=mesg, lat=lat, long=lon)
                            self.log(f'Spotted id{sid}: {mesg}')
                        except TwythonError as e:
                            self.log(e)

                    q = 'insert or replace into potaspots(utc, time,callsign,freq,mode,ref, park, comment) values(?, ?, ?, ?, ?, ?, ?, ?)'
                    try:
                        rfreq = float(freq)
                    except ValueError:
                        rfreq = 0.0

                    self.cur.execute(q, (self.now, hhmm, activator, rfreq, mode, ref, park, comment))

                if spots:
                    self.lastid = max(int(i['spotId']) for i in spots)
                    self.log(f'Latest spot id{self.lastid}.')
                else:
                    self.log(f'No spots since id{self.lastid}.')
                self.db.commit()

            else:
                self.log(f'No Spots')

            self.check_dm()

            with open(self.homedir + 'lastid.pkl', mode='wb') as f:
                pickle.dump({'lastid':self.lastid, 'lastmsg':self.lastmsg}, f)

            time.sleep(self.interval)

if __name__ == "__main__":
  spotter = POTASpotter(myid, 80, 'JA-*')
  spotter.run()
