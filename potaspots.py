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
from twython import Twython, TwythonError

consumer = ''
consumer_sec = ''
access = ''
access_sec = ''
myid =  ''

class POTASpotter:
    def __init__(self, myid, interval=70, prefix = 'JA-*'):
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
        self.cur2 = self.db.cursor()

        try:
            with open(self.homedir + 'lastid.pkl', mode='rb') as f:
                saved = pickle.load(f)
                self.lastid = saved['lastid']
                self.lastmsg = saved['lastmsg']
        except Exception as e:
            self.lastid = 0
            self.lastmsg = 0
            
        q = 'create table if not exists potaspots(utc int, time text, callsign text, freq real, mode text, region text, ref text, park text, comment text)'
        self.cur.execute(q)
        q = 'create index if not exists pota_index on potaspots(utc, callsign, region, ref)'
        self.cur.execute(q)


    def log(self, mesg):
        now = datetime.datetime.now()
        with open(self.logdir + self.logfile, mode='a') as f:
            print(f'{now}: {mesg}', file=f)

    def logsearch(self, target, twindow):
        lastseen = self.now - twindow
        mesg = ''
        
        q = f"select distinct callsign, ref from potaspots where utc > {lastseen}"
        if target:
            q += f" and region = '{target}'"

        for i in self.cur.execute(q):
            (call, ref) = i
            time_in, mode_in, freq_in = None, None, None
            time_out, mode_out, freq_out = None, None, None
            nfer = []
            sota = ''
            q = f"select * from potaspots where callsign = '{call}' and ref = '{ref}' and utc > {lastseen}"
            if target:
                q += f" and region = '{target}'"

            for j in self.cur2.execute(q):
                (_, tm, _, freq, mode, region, _, park, comment ) = j
                for cm in re.split('[, ;]', comment):
                    m = re.match('(\w+-\d\d\d\d)', cm)
                    if m :
                        nfer.append(m.group(1))

                    m = re.match('(\w+/\w+-\d+)', cm)
                    if m :
                        sota = m.group(1)
                        
                if mode == '':
                    mode = '*'

                if not time_in:
                    time_in = tm
                    mode_in = mode
                    freq_in = int(freq/1000)
                else:
                    time_out = tm
                    mode_out = mode
                    freq_out = int(freq/1000)

            if not time_out:
                tm = f"{time_in}"
                fr = f"{freq_in}({mode_in})"
            else:
                tm = f"{time_in}-{time_out}"
                fr = f"{freq_in}({mode_in})-{freq_out}({mode_out})"
        
            if nfer:
                refs = ' ' + str(len(nfer)+1) + '-fer: ' + '/'.join(nfer)
            else:
                refs = ''

            if sota != '':
                refs += ' SOTA:' + sota

            mesg += f"{tm} {call} {ref}{refs} {fr}\n"

        if mesg == '':
            mesg = 'No Logs.'

        return mesg
    
    def spotsearch(self, target, maxfreq, twindow):

        lastseen = self.now - twindow

        if maxfreq:
            q = f"select distinct callsign,ref  from potaspots where utc > {lastseen} and freq <= {maxfreq}"
        else:
            q = f"select distinct callsign, ref from potaspots where utc > {lastseen}"

        if target:
            q += f" and region = '{target}'"
            
        mesg = ''
        for s in self.cur.execute(q + ' order by utc desc'):
            (call, ref ) = s
            q = f"select * from potaspots where callsign ='{call}' and ref='{ref}' and utc > {lastseen} order by utc desc"
            l = self.cur2.execute(q)
            e = l.fetchone()
            (_, tm, call, freq, mode, region, ref, park, comment) = e
            mesg += f"{tm} {ref} {call} {freq} {mode} {comment}\n"

        if mesg == '':
            mesg = 'No Spots.'

        return mesg
    
    def interp(self, cmd):
        self.now = int(datetime.datetime.utcnow().strftime("%s"))
        command = cmd.upper().split()
        (region, maxfreq, logmode, twindow) = ('JA', None, False, 3600)
        for cmd in command:
            if 'JA' in cmd:
                region = 'JA'
                maxfreq = None
            elif 'DX' in cmd:
                region = None
                maxfreq = 100000
            elif 'LOG' in cmd:
                logmode = True
                twindow = 12 * 3600 #12H
            elif cmd.isdigit():
                twindow = int(cmd) * 60 
            else:
                break

        if logmode:
            mesg = self.logsearch(region, twindow)
        else:
            mesg = self.spotsearch(region, maxfreq, twindow)

        return mesg

    def check_dm(self):
        try:
            res = self.api.get_direct_messages()
        except TwythonError as e:
            self.log(f'Warning: {e.error_code}')
            return
        
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
                try:
                    self.api.send_direct_message(event = msgevent)
                    self.log(msgevent)
                except TwythonError as e:
                    self.log(f'Warning: {e.error_code}')
                    return

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
                            self.log(f'Error: {e}')
                            
                    region = ref[0:ref.find('-')]                              
                    try:
                        rfreq = float(freq)
                    except ValueError:
                        rfreq = 0.0
                    q = 'insert into potaspots(utc, time,callsign,freq,mode,region, ref, park, comment) values(?, ?, ?, ?, ?, ?, ?, ?, ?)'
                    self.cur.execute(q, (self.now, hhmm, activator, rfreq, mode, region, ref, park, comment))
                    
                    tlwindow = self.now - 3600 * 24 * 7
                    self.cur.execute(f'delete from potaspots where utc < {tlwindow}')

                if spots:
                    self.lastid = max(int(i['spotId']) for i in spots)
                    self.log(f'Latest spot id{self.lastid}.')
                else:
                    self.log(f'No spots since id{self.lastid}.')

                self.db.commit()
                
            else:
                self.log(f'No Spots.')

            self.check_dm()

            with open(self.homedir + 'lastid.pkl', mode='wb') as f:
                pickle.dump({'lastid':self.lastid, 'lastmsg':self.lastmsg}, f)

            time.sleep(self.interval)

if __name__ == "__main__":
  spotter = POTASpotter(myid, 70, 'JA-*')
  spotter.run()
