# coding: utf-8
from datetime import datetime
import pickle
import urllib.request
import json
import math
from paho.mqtt import client as mqtt
import os
import pytz
import re
import requests
import schedule
import ssl
import sys
import sqlite3
import time
import toml

import tweepy
from mastodon import Mastodon
from nostr.event import Event
from nostr.relay_manager import RelayManager
from nostr.message_type import ClientMessageType
from nostr.key import PrivateKey


class MDSpotter:
    def __init__(self, **args):

        self.programs = args.get('programs', None)
        self.accesskeys = args.get('accesskeys', None)
        self.endpoints = args.get('endpoints', None)
        self.translates = args.get('translates', None)
        self.config = args.get('config', None)
        self.endpoints = args.get('endpoints', None)

        self.localtz = pytz.timezone(self.config['localtz'])

        self.lastid = {}
        self.twapi = {}
        self.mdapi = {}
        self.ntpriv_key = {}
        self.ntrelay = None

        for p in self.programs:
            if self.accesskeys[p]['bearer']:
                k = self.accesskeys[p]
                self.twapi[p] = tweepy.Client(
                    bearer_token=k['bearer'],
                    consumer_key=k['consumer'],
                    consumer_secret=k['consumer_sec'],
                    access_token=k['access'],
                    access_token_secret=k['access_sec'])
            else:
                self.twapi[p] = None

            if self.accesskeys[p]['mastodon_access_token']:
                k = self.accesskeys[p]
                self.mdapi[p] = Mastodon(access_token=k['mastodon_access_token'],
                                         api_base_url=k['mastodon_api_base_url']
                                         )
            else:
                self.mdapi[p] = None

            if self.accesskeys[p]['nostr_private_key']:
                self.ntpriv_key[p] = PrivateKey.from_nsec(
                    self.accesskeys[p]['nostr_private_key'])
                if not self.ntrelay:
                    self.ntrelay = RelayManager()
                    for r in self.config['nostr_relay_servers']:
                        self.ntrelay.add_relay(
                            r, ssl_options={"cert_reqs": ssl.CERT_NONE})
            else:
                self.ntpriv_key[p] = None

            self.lastid[p] = 0

        def mqtt_onconnect(client, userdata, flags, rc):
            if rc == 0:
                self.log("Connected to MQTT Broker")
            else:
                self.log("Failed to connect MQTT Broker rc={rc}")

        self.mqtt = mqtt.Client('SOTA-POTA-SpotService')
        self.mqtt.username_pw_set(
            self.config['mqttuser'], self.config['mqttpasswd'])
        if self.config['mqttcert']:
            self.mqtt.tls_set(ca_certs=self.config['mqttcert'])
        self.mqtt.on_connect = mqtt_onconnect
        self.mqtt.connect(self.config['mqttbroker'], self.config['mqttport'])
        self.mqtt.loop_start()
        
        self.db = sqlite3.connect(self.config['homedir'] + 'mdspots.db')
        self.cur = self.db.cursor()
        self.cur2 = self.db.cursor()
        self.now = int(datetime.utcnow().strftime("%s"))

        q = 'create table if not exists mdspots2(utc int, time text, prog text, callsign text, ' \
            'ref txt, name text, freq real, rawfreq text, mode text, loc text, region text, comment text, spotter text, tweeted int)'
        self.cur.execute(q)
        q = 'create index if not exists md_reg_index on mdspots2(utc, prog, region)'
        self.cur.execute(q)
        q = 'create index if not exists md_ref_index on mdspots2(utc, ref)'
        self.cur.execute(q)
        q = 'create index if not exists md_call_index on mdspots2(utc, callsign)'
        self.cur.execute(q)

        self.db.commit()
        self.loadLastId()

    def __del__(self):
        self.db.commit()
        self.db.close()

        if self.ntrelay:
            self.ntrelay.close_all_relay_connections()

    def saveLastId(self):
        with open(self.config['homedir'] + 'lastid.pkl', mode='wb') as f:
            pickle.dump(self.lastid, f)

    def loadLastId(self):
        try:
            with open(self.config['homedir'] + 'lastid.pkl', mode='rb') as f:
                saved = pickle.load(f)
                for p in self.programs:
                    self.lastid[p] = saved[p]
        except Exception as e:
            self.log("Info: lastid.pkl not found.")

    def log(self, mesg):
        now = datetime.now()
        with open(self.config['logdir'] + self.config['logname'], mode='a') as f:
            print(f'{now}: {mesg}', file=f)

    def mqtt_publish(self, topic, mesg):
        res = self.mqtt.publish(topic, mesg)
        self.log(f"MQTTPublish({res}): {mesg} to {topic}")
        
    def tweet_as_reply(self, prog, repl_id, mesg):

        if not self.twapi[prog]:
            self.log(f"Tweet {prog} ={mesg}")
            return None

        if not repl_id:
            try:
                res = self.twapi[prog].create_tweet(text=mesg)
                self.log(f'Spotted: {mesg}')
            except Exception as e:
                self.log(f'Warning: {prog} {mesg} : {e}')
                return None
        else:
            try:
                res = self.twapi[prog].create_tweet(
                    text=mesg, in_reply_to_tweet_id=repl_id['id'])
                self.log(f'Spotted: {mesg}')
            except Exception as e:
                self.log(f'Warning:{prog} {mesg} {e}')
                return None

        return res.data

    def toot_as_reply(self, prog, repl_id, mesg):

        if not self.mdapi[prog]:
            self.log(f"Toot={mesg}")
            return None

        if not repl_id:
            try:
                res_md = self.mdapi[prog].status_post(mesg)
                self.log(f'SpottedMD: {mesg}')
            except Exception as e:
                self.log(f'ErrorMD: {e}')
                res_md = None
        else:
            try:
                res_md = self.mdapi[prog].status_post(
                    mesg, in_reply_to_id=repl_id['id'])
                self.log(f'SpottedMD: {mesg}')
            except Exception as e:
                self.log(f'ErrorMD: {e}')
                res_md = None

        return res_md

    def post_nostr_event(self, prog, repl_id, mesg):
        if not self.ntpriv_key[prog]:
            self.log(f"Nostr({prog})={mesg}")
            return None

        res_nostr = None

        if self.ntrelay:
            event = Event(mesg)
            if repl_id:
                event.add_event_ref(repl_id)

            self.ntpriv_key[prog].sign_event(event)
            res_nostr = event.id
            self.ntrelay.publish_event(event)
            time.sleep(1)

        return res_nostr

    def close_connection(self):
        if self.ntrelay:
            self.ntrelay.close_connections()
            self.log(
                f"Closed NOSTR Relay Servers:{self.config['nostr_relay_servers']}")

    def getJSON(self, prog, ty, param=None):
        if self.endpoints[prog][ty]:
            try:
                if not param:
                    param = urllib.parse.urlencode(
                        self.endpoints[prog]['client'])
                else:
                    param = urllib.parse.urlencode({'refid': param})

                readObj = urllib.request.urlopen(
                    self.endpoints[prog][ty] + param)
                res = readObj.read()
                return json.loads(res)
            except Exception as e:
                self.log(f'Error:{e} {self.endpoints[prog][ty]}')
                raise e

    def refNamequery(self, refid):
        m = re.match(r'JA*', refid)
        if m:
            res = self.getJSON('sotalive', 'getref', refid)
            if res['counts'] == 0:
                return (refid, None)
            else:
                name = res['reference'][0]['name']
                name_k = re.sub(r'\(.+\)|（.+）', '', res['reference'][0]['name_k'])
                return (name, name_k)
        else:
            return (refid, None)
        
    def freqstr(self, f):
        if f < 4000:
            s = f"{f/1000:.1f}"
        else:
            s = f"{math.floor(f/1000):.0f}"
        return s

    def summary_mesg(self, call, t, sc, rc, mesg):
        def pl(x): return 's' if x > 1 else ''
        t = int(t)
        if call:
            return f"Activation summary for the last {t} hour{pl(t)}: {call} activated {rc} reference{pl(rc)}.\n" + mesg
        elif sc > 0:
            return f"Activation summary for the last {t} hour{pl(t)}: {sc} station{pl(sc)} activated {rc} reference{pl(rc)}.\n" + mesg
        else:
            return f"Activation summary for the last {t} hour{pl(t)}: No activation."

    def logsearch(self, prog, region, locpfx, call, twindow):
        lastseen = self.now - twindow
        mesg = ''
        references = set()
        stations = set()

        if region:
            q_reg = f"region like '{region}%' and"
        else:
            q_reg = ""

        if call:
            q_call = f"callsign like '{call}%' and"
        else:
            q_call = ""

        q = f"select distinct callsign, ref from mdspots2 where prog = '{prog}' and {q_reg} {q_call} utc > {lastseen}"

        for i in self.cur.execute(q):
            (call, ref) = i
            time_in, mode_in, freq_in = None, None, None
            time_out, mode_out, freq_out = None, None, None
            nfer = []
            sota = ''
            mloc = ''
            lastmode = None

            q = f"select time, freq, mode, comment, spotter from mdspots2 where prog = '{prog}' and callsign = '{call}' and ref = '{ref}' and utc > {lastseen}"

            for j in self.cur2.execute(q):
                (tm, freq, mode, comment, spotter) = j
                if spotter and spotter in call:
                    if comment:
                        cmlist = re.split(r'[, :;]', comment)
                        isnfer = re.search(r'fer', comment, re.IGNORECASE)
                    else:
                        cmlist = []
                        isnfer = None

                    for cm in cmlist:
                        m = re.match(r'(\w+-\d\d\d\d)', cm)
                        if isnfer and m:
                            ref2 = m.group(1)
                            if not 'FF' in ref2 and ref != ref2 and not ref2 in nfer:
                                nfer.append(ref2)
                                references.add(ref2)

                        m = re.match(r'(\w+/\w+-\d+)', cm)
                        if m:
                            sota = m.group(1)

                        if locpfx:
                            m = re.match(f"({locpfx}-\D+)", cm)
                            if m:
                                mloc = m.group(1)

                if not time_in:
                    time_in = tm
                    freq_in = self.freqstr(freq)
                    if mode:
                        mode_in = mode
                        lastmode = mode
                else:
                    time_out = tm
                    freq_out = self.freqstr(freq)
                    if mode:
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
                if prog == 'pota':
                    refs += ' ' + str(len(nfer)+1) + '-fer:' + '/'.join(nfer)
                else:
                    refs += ' ' + 'POTA:' + '/'.join(nfer)

            if sota and prog == 'pota':
                refs += ' SOTA:' + sota

            mesg += f"{tm} {call} {ref}{refs} {fr}\n"

            stations.add(call)
            references.add(ref)

        return (len(stations), len(references), mesg)

    def spotsearch(self, prog, region, call, maxfreq, twindow):
        lastseen = self.now - twindow
        mesg = ''
        count = 0

        if region:
            q_reg = f"region = '{region}' and"
        else:
            q_reg = ""

        if call:
            q_call = f"callsign like '{call}%' and"
        else:
            q_call = ""

        if maxfreq:
            q_freq = f"freq <= {maxfreq} and"
        else:
            q_freq = ""

        q = f"select distinct callsign,ref from mdspots2 where prog = '{prog}' and {q_reg} {q_call} {q_freq} utc > {lastseen}"

        for s in self.cur.execute(q + ' order by utc desc'):
            (call, ref) = s
            q = f"select time,callsign,freq,mode,comment from mdspots2 where callsign ='{call}' and ref='{ref}' and utc > {lastseen} order by utc desc"
            l = self.cur2.execute(q)
            e = l.fetchone()
            (tm, call, freq, mode, comment) = e
            if (mode == comment):
                comment = ''
            mesg += f"{tm} {ref} {call} {freq} {mode} {comment}\n"
            count += 1

        if count == 0:
            mesg = 'No Spots.'

        return (count, mesg)

    def stats(self, prog, region, call, mode, now, twindow):
        if region:
            q_reg = f"region like '{region}%' and"
        else:
            q_reg = ""

        if call:
            q_call = f"callsign like '{call}%' and"
        else:
            q_call = ""

        if mode:
            q_mod = f"mode = '{mode}' and"
        else:
            q_mod = ""

        reg_q_all = f"select ref,callsign,count(callsign) from mdspots2' \
            ' where prog = '{prog}' and {q_reg} {q_call} {q_mod} utc > {now - twindow} group by ref, callsign order by utc"

        reg_q_tweet = f"select ref,callsign,count(callsign) from mdspots2' \
            ' where prog = '{prog}' and {q_reg} {q_call} {q_mod} tweeted = 1 and utc > {now - twindow} group by ref, callsign"

        refmap = {}

        (twtall, spotall) = (0, 0)
        for s in self.cur.execute(reg_q_all):
            (ref, call, count) = s
            spotall += count
            if not ref in refmap:
                refmap[ref] = {call: (0, count)}
            else:
                refmap[ref][call] = (0, count)

        for s in self.cur.execute(reg_q_tweet):
            (ref, call, count) = s
            twtall += count
            if ref in refmap and call in refmap[ref]:
                (_, total) = refmap[ref][call]
                refmap[ref][call] = (count, total)

        if spotall != 0:
            if mode:
                mstr = f"({mode})"
            else:
                mstr = ""

            mesg = f"Tweet Rate Last {round(twindow/3600)}hrs {mstr} = {round(twtall/spotall*100)}%({twtall}tweets/{spotall}spots)\n"

            for ref in refmap.keys():
                mesg += f"{ref}: "
                for call in refmap[ref].keys():
                    (twt, total) = refmap[ref][call]
                    mesg += f"{call} {round(twt/total*100)}%({twt}/{total}) "
                mesg += "\n"
            mesg = mesg.rstrip()
        else:
            mesg = f"No spots in {region}"

        return mesg

    def interp(self, cmd):
        self.now = int(datetime.utcnow().strftime("%s"))
        command = cmd.upper().split()
        prog = self.programs[0]
        (region, locpfx, call, mode, maxfreq, logmode, statmode, twindow) = (
            'JA', None, None, None, None, False, False, 3600)

        for cmd in command:
            if cmd == 'JA':
                region = 'JA'
                locpfx = 'JP'
                maxfreq = None
            elif cmd == 'DX':
                region = None
                maxfreq = 100000
            elif cmd == 'LOG':
                logmode = True
                twindow = 12 * 3600
            elif cmd == 'STAT':
                statmode = True
                twindow = 24 * 3600
            elif cmd in ['FT4', 'FT8', 'CW', 'SSB', 'FM', 'AM', 'PSK', 'PSK31']:
                mode = cmd
            elif cmd in ['SOTA', 'POTA']:
                prog = cmd.lower()
            elif cmd.isdigit():
                if logmode or statmode:
                    twindow = int(cmd) * 3600
                else:
                    twindow = int(cmd) * 60
            elif cmd.isalnum():
                if len(cmd) > 3:
                    call = cmd
                else:
                    region = cmd

        if statmode:
            mesg = self.stats(prog, region, call, mode, self.now, twindow)

        elif logmode:
            (stns, refs, mesg) = self.logsearch(
                prog, region, locpfx, call, twindow)
            mesg = self.summary_mesg(call, twindow/3600, stns, refs, mesg)

        else:
            (_, mesg) = self.spotsearch(prog, region, call, maxfreq, twindow)

        return mesg

    # def check_dm(self, prog):
    #     try:
    #         res = self.twapi[prog].get_direct_messages()
    #     except TwythonError as e:
    #         self.log(f'Warning: {e} get_direct_messagge')
    #         return

    #     msglist = [m for m in res["events"] if int(
    #         m["created_timestamp"]) > self.lastmsg]
    #     if msglist:
    #         self.lastmsg = max(int(i['created_timestamp']) for i in msglist)

    #     msglist.sort(key=(lambda x: x['created_timestamp']))

    #     for usr in msglist:
    #         rcpt = usr["message_create"]["sender_id"]
    #         if rcpt != self.myid:
    #             cmd = usr["message_create"]["message_data"]["text"]
    #             mesg = self.interp(cmd)
    #             msgevent = {
    #                 "type": "message_create",
    #                 "message_create": {"target": {"recipient_id": rcpt},
    #                                    "message_data":
    #                                    {"text": mesg}}}
    #             try:
    #                 self.api.send_direct_message(event=msgevent)
    #                 self.log(f"Message: cmd='{cmd}' res={msgevent}")
    #             except TwythonError as e:
    #                 self.log(f'Warning: {e} send_direct_message')
    #                 return

    def summary(self, prog):
        (stns, refs, mesg) = self.logsearch(prog, 'JA',
                                            'JP', None, self.config[prog]['summary'] * 3600)
        mesg = self.summary_mesg(
            None, self.config[prog]['summary'], stns, refs, mesg)
        mesg_md = mesg

        res = None
        tm = ''
        if stns > 0:
            for m in mesg.splitlines():
                if len(tm + m) > 270:
                    res = self.tweet_as_reply(prog, res, tm.rstrip())
                    tm = m + '\n'
                else:
                    tm += m + '\n'
            mesg = tm
        self.tweet_as_reply(prog, res, mesg.rstrip())

        res = None
        tm = ''
        if stns > 0:
            for m in mesg_md.splitlines():
                if len(tm + m) > 490:
                    res = self.toot_as_reply(prog, res, tm.rstrip())
                    tm = m + '\n'
                else:
                    tm += m + '\n'
            mesg = tm
        self.toot_as_reply(prog, res, mesg.rstrip())

        res = None
        tm = ''
        if stns > 0:
            for m in mesg_md.splitlines():
                if len(tm + m) > 2048:
                    res = self.post_nostr_event(prog, res, tm.rstrip())
                    tm = m + '\n'
                else:
                    tm += m + '\n'
            mesg = tm
        self.post_nostr_event(prog, res, tm.rstrip())

    def is_selfspot(self, spotter, activator):
        sp = re.sub('-\d+|/\d+|/P', '', spotter.upper())
        return sp in activator.upper()

    def alerts(self, prog):
        self.now = int(datetime.utcnow().strftime("%s"))

        alert_from = self.now - 3600 * 4
        alert_to = self.now + 3600 * 12

        try:
            alertdata = self.getJSON(prog, 'alerts')
        except Exception as e:
            self.log(f"Warning:{e} {self.endpoints[prog]['alerts']}")
            return

        res = []
        if alertdata:
            tr = self.translates['alerts'][prog]
            for a in alertdata:
                ref = '/'.join(a[x] for x in tr['ref'])
                m = re.match(self.config[prog]['filter'], ref)
                if m:
                    dt = datetime.fromisoformat(
                        'T'.join(a[x] for x in tr['time']))
                    st = int(dt.strftime("%s"))
                    if st >= alert_from and st <= alert_to:
                        mesg = f"{dt.strftime('%b%d %H:%M')} {a[tr['act']]} on\n{ref} {a[tr['freq']]}\n" \
                            f"{a[tr['name']]}\n"
                        if a[tr['comments']]:
                            mesg += a[tr['comments']]
                        mesg += "\n"
                        res.append(mesg)

        acount = len(res)
        today = datetime.now(self.localtz).strftime("%A %d %B %Y")
        if acount == 0:
            tm = f'No Activations are scheduled on {today}'
        elif acount == 1:
            tm = f'An Activation is scheduled on {today}'
        else:
            tm = f'{acount} Activations are scheduled on {today}'

        res.insert(0, tm)
        return res

    def spots(self, prog):
        self.now = int(datetime.utcnow().strftime("%s"))

        try:
            spotdata = self.getJSON(prog, 'spots')
        except Exception as e:
            self.log(f"Warning:{e} {self.endpoints[prog]['spots']}")
            return

        if spotdata:
            tr = self.translates['spots'][prog]
            spots = [s for s in spotdata[::-1]
                     if int(s[tr['id']]) > self.lastid[prog]]
            for s in spots:
                sid = s[tr['id']]
                ref = '/'.join(s[x] for x in tr['ref'])
                activator = s[tr['act']]
                freq = s[tr['freq']]
                mode = s[tr['mode']]
                name = s[tr['name']]
                loc = s[tr['loc']]
                spotter = s[tr['spotter']]
                comment = s[tr['comments']]

                tstr = re.sub(r'\.\d+', r'', s[tr['time']])
                hhmm = datetime.fromisoformat(tstr).strftime('%H:%M')

                if not spotter:
                    continue

                if not comment:
                    comment = ''

                if (mode == comment):
                    comment = ''

                m = re.match('(\w+)[-|/]', ref)
                if m:
                    region = m.group(1)
                else:
                    region = None

                try:
                    f = float(freq)
                    if prog == 'sota':
                        f = f * 1000
                    if f > 30000.0:
                        m = 10
                    else:
                        m = 20
                    if 'FT' in mode or 'JT' in mode:
                        rfreq = round(f / m, 0) * m
                    else:
                        rfreq = round(f, 0)
                except ValueError:
                    rfreq = 0.0

                if not self.is_selfspot(spotter, activator):
                    q = f"select count(*) from mdspots2 where prog = '{prog}' and " \
                        f"utc > {self.now - self.config[prog]['suppress_interval']} and " \
                        f"callsign = '{activator}' and ref = '{ref}' and " \
                        f"freq = {rfreq} and mode = '{mode}' and tweeted = 1"

                    self.cur.execute(q)
                    (count,) = self.cur.fetchall()[0]
                    if count == 0:
                        skip_this = False
                    else:
                        skip_this = True
                else:
                    skip_this = False


                if not skip_this:
                    if prog == 'pota':
                        (_, name_k) = self.refNamequery(ref)
                        if name_k:
                            mesg = f'{hhmm} {activator} on {ref}({name_k} {name}, {loc}) {freq} {mode} {comment}[{spotter}]'
                        else:
                            mesg = f'{hhmm} {activator} on {ref}({name}, {loc}) {freq} {mode} {comment}[{spotter}]'
                    else:
                        mesg = f'{hhmm} {activator} on {ref}({name}) {freq} {mode} {comment}[{spotter}]'

                    m = re.match(self.config[prog]['filter'], ref)
                    if m:
                        if self.config[prog]['enable_tweet']:
                            self.tweet_as_reply(prog, None, mesg)
                        
                        if self.config[prog]['enable_toot']:
                            self.toot_as_reply(prog, None, mesg)
                        
                        if self.config[prog]['enable_nostr']:
                            self.post_nostr_event(prog, None, mesg)

                    if self.config[prog]['enable_mqtt']:
                        it = iter(self.config[prog]['mqtt_topic'])
                        for topic, pat in zip(it, it):
                            m = re.match(pat, ref)
                            if m:
                                self.mqtt_publish(topic, mesg)
                    
                q = 'insert into mdspots2(utc, time, prog, callsign, ref, name, freq, rawfreq, mode, loc, region, comment, spotter, tweeted) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
                self.cur.execute(q, (self.now, hhmm, prog, activator, ref, name,
                                     rfreq, freq, mode, loc, region, comment, spotter, 0 if skip_this else 1))

                tlwindow = self.now - 3600 * 24 * \
                    self.config[prog]['storage_period']
                self.cur.execute(
                    f'delete from mdspots2 where utc < {tlwindow}')

            if spots:
                self.lastid[prog] = max(int(i[tr['id']]) for i in spots)
                self.log(f'Latest {prog} spot id{self.lastid[prog]}.')
            else:
                self.log(f'No {prog} spots since id{self.lastid[prog]}.')

            self.db.commit()

        else:
            self.log(f'No {prog} spots.')

        self.saveLastId()

    def periodical(self):
        for p in self.programs:
            self.spots(p)

    def daily_alerts(self):
        for p in self.programs:
            rest = None
            resm = None
            resn = None
            for a in self.alerts(p):
                rest = self.tweet_as_reply(p, rest, a)
                resm = self.toot_as_reply(p, resm, a)
                resn = self.post_nostr_event(p, resn, a)

    def daily_summary(self):
        for p in self.programs:
            self.summary(p)

    def run(self):
        self.log(f"Start MDSpot Server {__file__}")

        schedule.every(self.config['interval']).seconds.do(self.periodical)
        schedule.every().day.at(self.config['alerts']).do(self.daily_alerts)
        schedule.every().day.at(self.config['summary']).do(self.daily_summary)

        while True:
            schedule.run_pending()
            time.sleep(10)


if __name__ == "__main__":
    with open(os.path.dirname(__file__) + '/mdspots.toml') as f:
        systemobj = toml.load(f)

    with open(os.path.dirname(__file__) + '/config.toml') as f:
        configobj = toml.load(f)

    translates = {
        'alerts': systemobj['alert_translates'], 'spots': systemobj['spot_translates']}

    spotter = MDSpotter(programs=systemobj['programs'],
                        endpoints=systemobj['endpoints'],
                        translates=translates,

                        accesskeys=configobj['accesskeys'],
                        config=configobj['config'])

    if len(sys.argv) == 1:
        spotter.run()
    else:
        command = sys.argv[1]
        if command == 'create_app':
            appname = input('Application Name:')
            baseurl = input('Mastodon API Base URL:')
            tofile = input('Client Credential File:')
            Mastodon.create_app(appname,
                                api_base_url=baseurl,
                                to_file=tofile
                                )

        elif command == 'create_token':
            clientsecret = input('Client Credential File:')
            account = input('Mastodon Account:')
            password = input('Mastodon Password:')
            tofile = input('Access Token File:')
            mastodon = Mastodon(client_id=clientsecret,)
            mastodon.log_in(
                account,
                password,
                to_file=tofile
            )
        else:
            print(spotter.interp(' '.join(sys.argv[1:])))
            del spotter
