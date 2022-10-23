#!/usr/bin/env python3
# coding: utf-8
import datetime
from os import access
import pickle
import urllib.request
import json
import math
import re
import schedule
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
    def __init__(self, myid, interval=70, suppress_interval=600, storage_period = 7, tweetat="21:00", logwindow=21, prefix='JA-*'):
        self.myid = myid;
        self.interval = interval
        self.suppress_interval = suppress_interval
        self.storage_period = storage_period
        self.tweetat = tweetat
        self.logwindow = logwindow
        self.prefix = prefix
        self.potaapi = 'https://api.pota.app/spot/activator/'
        self.logdir = '/var/log/potaspots/'
        self.homedir = '/home/ubuntu/sotaapp/backend/'
        self.logfile = 'potaspot.log'
        self.api = Twython(consumer, consumer_sec, access, access_sec)
        self.db = sqlite3.connect(self.homedir + 'potaspot.db')
        self.cur = self.db.cursor()
        self.cur2 = self.db.cursor()
        self.now = int(datetime.datetime.utcnow().strftime("%s"))

        try:
            with open(self.homedir + 'lastid.pkl', mode='rb') as f:
                saved = pickle.load(f)
                self.lastid = saved['lastid']
                self.lastmsg = saved['lastmsg']
        except Exception as e:
            self.lastid = 0
            self.lastmsg = 0
            
        q = 'create table if not exists potaspots(utc int, time text, callsign text, freq real, mode text, region text, ref text, park text, comment text, spotter text, tweeted int)'
        self.cur.execute(q)
        q = 'create index if not exists pota_index on potaspots(utc, callsign, region, ref)'
        self.cur.execute(q)


    def log(self, mesg):
        now = datetime.datetime.now()
        with open(self.logdir + self.logfile, mode='a') as f:
            print(f'{now}: {mesg}', file=f)

    def tweet_as_reply(self, repl_id, mesg):
        if not repl_id:
            try:
                res = self.api.update_status(status=mesg)
                self.log(f'Spotted: {mesg}')
            except TwythonError as e:
                self.log(f'Error: {e}')
                res = None
        else:
            try:
                res = self.api.update_status(status=mesg, in_reply_to_status_id=repl_id, auto_populate_reply_metadata=True)
                self.log(f'Spotted: {mesg}')
            except TwythonError as e:
                self.log(f'Error: {e}')
                res = None
        if res:
            return res['id']
        else:
            return None

    def freqstr(self, f):
        if f < 4000:
            s = f"{f/1000:.1f}"
        else:
            s = f"{math.floor(f/1000):.0f}"
        return s

    def summary_mesg(self, t, sc, rc, mesg):
        pl = lambda x: 's' if x > 1 else ''
        t = int(t)
        if sc > 0:
            return f"Activation summary for the last {t} hour{pl(t)}: {sc} station{pl(sc)} activated {rc} park{pl(rc)}.\n" + mesg
        else:
            return  f"Activation summary for the last {t} hour{pl(t)}: No activation."

    def logsearch(self, target, locpfx, twindow):
        lastseen = self.now - twindow
        mesg = ''
        references = set()
        stations = set()
        
        q = f"select distinct callsign, ref from potaspots where utc > {lastseen}"
        if target:
            q += f" and region = '{target}'"

        for i in self.cur.execute(q):
            (call, ref) = i
            time_in, mode_in, freq_in = None, None, None
            time_out, mode_out, freq_out = None, None, None
            nfer = []
            sota = ''
            mloc = ''
            lastmode = None
            
            q = f"select * from potaspots where callsign = '{call}' and ref = '{ref}' and utc > {lastseen}"
            if target:
                q += f" and region = '{target}'"

            for j in self.cur2.execute(q):
                (_, tm, _, freq, mode, region, _, park, comment, spotter, _) = j
                if spotter and spotter in call:
                    cmlist = re.split('[, :;]', comment)
                    isnfer = re.search('fer', comment, re.IGNORECASE)
                    for cm in cmlist:
                        m = re.match('(\w+-\d\d\d\d)', cm)
                        if isnfer and m:
                            ref2 = m.group(1)
                            if not 'FF' in ref2 and ref != ref2 and not ref2 in nfer:
                                nfer.append(ref2)
                                references.add(ref2)

                        m = re.match('(\w+/\w+-\d+)', cm)
                        if m:
                            sota = m.group(1)

                        if locpfx:
                            m = re.match(f"({locpfx}-\D+)", cm)
                            if m:
                                mloc = m.group(1)

                if not time_in:
                    time_in = tm
                    freq_in = self.freqstr(freq)
                    if mode :
                        mode_in = mode
                        lastmode = mode
                else:
                    time_out = tm
                    freq_out = self.freqstr(freq) 
                    if mode :
                        mode_out = mode
                        lastmode = mode
                        if not mode_in:
                            mode_in = mode
                    else:
                        mode_out = lastmode

            if not mode_in:
                mode_in = '*'

            if not mode_out:
                mode_out = '*'
                
            if not time_out:
                tm = f"{time_in}"
                fr = f"{freq_in}({mode_in})"
            else:
                tm = f"{time_in}-{time_out}"
                fr = f"{freq_in}({mode_in})-{freq_out}({mode_out})"

            if mloc:
                refs = ' Loc:' + mloc
            else:
                refs = ''
        
            if nfer:
                refs +=  ' ' + str(len(nfer)+1) + '-fer:' + '/'.join(nfer)

            if sota:
                refs += ' SOTA:' + sota

            mesg += f"{tm} {call} {ref}{refs} {fr}\n"

            stations.add(call)
            references.add(ref)
            
        return (len(stations), len(references), mesg)
    
    def spotsearch(self, target, maxfreq, twindow):
        lastseen = self.now - twindow
        mesg = ''
        count = 0

        if maxfreq:
            q = f"select distinct callsign,ref  from potaspots where utc > {lastseen} and freq <= {maxfreq}"
        else:
            q = f"select distinct callsign, ref from potaspots where utc > {lastseen}"

        if target:
            q += f" and region = '{target}'"
            
        for s in self.cur.execute(q + ' order by utc desc'):
            (call, ref ) = s
            q = f"select * from potaspots where callsign ='{call}' and ref='{ref}' and utc > {lastseen} order by utc desc"
            l = self.cur2.execute(q)
            e = l.fetchone()
            (_, tm, call, freq, mode, region, ref, park, comment, spotter, _) = e
            if (mode == comment):
                    comment = ''
            mesg += f"{tm} {ref} {call} {freq} {mode} {comment}\n"
            count += 1
            
        if count == 0:
            mesg = 'No Spots.'

        return (count, mesg)
    
    def stats(self, region, now, twindow):
        if region:
            q_reg = f"region = '{region}' and"
        else:
            q_reg = ""
            
        reg_q_all = f"select ref,callsign,count(callsign) from potaspots where {q_reg} utc > {now - twindow} group by ref, callsign order by utc"
        
        reg_q_tweet = f"select ref,callsign,count(callsign) from potaspots where {q_reg} tweeted = 1 and utc > {now - twindow} group by ref, callsign"
        refmap= {}

        (twtall, spotall) = (0 , 0)
        for s in self.cur.execute(reg_q_all):
            (ref, call, count) = s
            spotall += count
            if not ref in refmap:
                refmap[ref] = {call:(0, count)}
            else:
                refmap[ref][call] = (0, count)
                    
        for s in self.cur.execute(reg_q_tweet):
            (ref, call, count) = s
            twtall += count
            if ref in refmap and call in refmap[ref]:
                (_, total) = refmap[ref][call]
                refmap[ref][call] = (count, total)

        mesg = f"Tweet Rate Last {round(twindow/3600)}H = {round(twtall/spotall*100)}%({twtall}tweets/{spotall}spots)\n"
        for ref in refmap.keys():
            mesg += f"{ref}: "
            for call in refmap[ref].keys():
                (twt, total) = refmap[ref][call]
                mesg += f"{call} {round(twt/total*100)}%({twt}/{total}) "
            mesg += "\n"

        return mesg;
        
    def interp(self, cmd):
        self.now = int(datetime.datetime.utcnow().strftime("%s"))
        command = cmd.upper().split()
        (region, locpfx, maxfreq, logmode, statmode, twindow) = ('JA', None, None, False, False, 3600)
        for cmd in command:
            if 'JA' in cmd:
                region = 'JA'
                locpfx = 'JP'
                maxfreq = None
            elif 'DX' in cmd:
                region = None
                maxfreq = 100000
            elif 'LOG' in cmd:
                logmode = True
                twindow = 12 * 3600
            elif 'STAT' in cmd:
                statmode = True
                twindow = 24 * 3600
            elif cmd.isalpha():
                region = cmd
            elif cmd.isdigit():
                if logmode or statmode:
                    twindow = int(cmd) * 3600
                else:
                    twindow = int(cmd) * 60
            else:
                break

        if statmode:
            mesg = self.stats(region, self.now, twindow)

        elif logmode:
            (stns, refs, mesg) = self.logsearch(region, locpfx, twindow)
            mesg = self.summary_mesg(twindow/3600, stns, refs, mesg)

        else:
            (_, mesg) = self.spotsearch(region, maxfreq, twindow)

        return mesg

    def check_dm(self):
        try:
            res = self.api.get_direct_messages()
        except TwythonError as e:
            self.log(f'Warning: {e} get_direct_messagge')
            return
        
        msglist = [ m for m in res["events"] if int(m["created_timestamp"]) > self.lastmsg ]
        if msglist:
            self.lastmsg = max(int(i['created_timestamp']) for i in msglist)

        msglist.sort(key=(lambda x: x['created_timestamp']))

        for usr in msglist:
            rcpt = usr["message_create"]["sender_id"]
            if rcpt != self.myid:
                cmd = usr["message_create"]["message_data"]["text"]
                mesg = self.interp(cmd)
                msgevent = {
                    "type": "message_create",
                    "message_create": { "target":{"recipient_id": rcpt },
                                        "message_data":
                                        {"text": mesg }}}
                try:
                    self.api.send_direct_message(event = msgevent)
                    self.log(f"Message: cmd='{cmd}' res={msgevent}")
                except TwythonError as e:
                    self.log(f'Warning: {e} send_direct_message')
                    return
            
    def summary(self):
        (stns, refs, mesg) = self.logsearch('JA', 'JP', self.logwindow * 3600)
        mesg = self.summary_mesg(self.logwindow, stns, refs, mesg)
        res = None
        tm = ''
        if stns > 0:
            for m in mesg.splitlines():
                if len(tm + m) > 270:
                    res = self.tweet_as_reply(res, tm.rstrip())
                    tm = m + '\n'
                else:
                    tm += m + '\n'
            mesg = tm
        self.tweet_as_reply(res, mesg.rstrip())

    def periodical(self):
        self.now = int(datetime.datetime.utcnow().strftime("%s"))
        
        try:
            spotobj = urllib.request.urlopen(self.potaapi)
            spotdata = json.loads(spotobj.read())
        except Exception as e:
            self.log(f'Warning:{e} {self.potaapi}')
            time.sleep(self.interval)
            return

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

                if not spotter:
                    continue
                
                if (mode == comment):
                    comment = ''

                region = ref[0:ref.find('-')]                              
                try:
                    if 'FT' in mode:
                        rfreq = round(float(freq)/10,0) * 10
                    else:
                        rfreq = round(float(freq),0)
                except ValueError:
                    rfreq = 0.0

                if not (spotter in activator):
                    q = f"select count(*) from potaspots where utc > {self.now - self.suppress_interval} and callsign = '{activator}' and ref = '{ref}' and freq = {rfreq} and mode = '{mode}' and tweeted = 1"
                    self.cur.execute(q)
                    (count,) = self.cur.fetchall()[0]
                    if count == 0:
                        skip_this = False
                    else:
                        skip_this = True
                else:
                    skip_this = False
                    
                m = re.match(self.prefix, ref)
                if not skip_this and m:
                    mesg = f'{hhmm} {activator} on {ref}({loc} {park}) {freq} {mode} {comment}[{spotter}]'
                    try:
                        res = self.api.update_status(status=mesg, lat=lat, long=lon)
                        self.log(f'Spotted id{sid}: {mesg}')
                    except TwythonError as e:
                        self.log(f'Warning:{e} status={mesg}')

                q = 'insert into potaspots(utc, time, callsign, freq, mode, region, ref, park, comment, spotter, tweeted) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
                self.cur.execute(q, (self.now, hhmm, activator, rfreq, mode, region, ref, park, comment, spotter, 0 if skip_this else 1))
                        
                tlwindow = self.now - 3600 * 24 * self.storage_period
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

    def run(self):
        schedule.every(self.interval).seconds.do(self.periodical)
        schedule.every().day.at(self.tweetat).do(self.summary)

        while True:
            schedule.run_pending()
            time.sleep(10)
        
if __name__ == "__main__":
  spotter = POTASpotter(myid = myid,
                        interval = 70,
                        suppress_interval= 1200,
                        storage_period = 31,
                        tweetat="21:00",
                        prefix='JA-*')
  spotter.run()
