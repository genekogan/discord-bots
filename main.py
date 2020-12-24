from dotenv import load_dotenv
from easydict import EasyDict
from datetime import datetime, timedelta, timezone
from suntime import Sun, SunTimeException
import sched
import asyncio
import itertools
import time
import math
import os
import re
import json
import random
import requests
import numpy as np

import discord
import spotipy
import gpt3

from programs import gpt3_chat
from programs import gpt3_prompt
from programs import ml4a_client
from programs import spotify

from bots import bots
from emojis import emoji_docs

botlist = ['sunrisesunset', 'mesa', 'mechanicalduck', 
           'chatsubo', 'wall-e', 'eve', 
           'facts', 'philosophy', 'deeplearning', 
           'kitchen', 'qa']

emoji_search_results = {}


def utc_to_local(utc_dt):
    return utc_dt.replace(tzinfo=timezone.utc).astimezone(tz=None)


class DiscordBot(discord.Client):
       
    async def setup(self, settings):
        self.ready = False
        self.settings = EasyDict(settings)
        self.timestamps = []
        self.last_senders = {}
        self.member2var = None
        self.var2member = None
        token = os.getenv(self.settings.token_env) 
        await self.start(token)
        
        
    async def on_ready(self):
        self.ready = True
        guild_names = [guild.name for guild in self.guilds]
        print('{} has connected to guilds: {}'.format(self.user, ','.join(guild_names)))
        if 'background' in self.settings.behaviors:
            self.loop.create_task(self.background_process())
        if 'timed' in self.settings.behaviors:
            self.loop.create_task(self.schedule_timed_events())
        
        
    async def update_member_lookup(self, message):
        channel = message.channel
        last_senders = self.last_senders[channel.id] if channel.id in self.last_senders else None
        
        if last_senders is None:
            message_history = await channel.history(limit=50).flatten()
            last_senders  = [member.id for member in message.guild.members]
            last_senders += [msg.author.id for msg in message_history[::-1]]
        else:
            last_senders += [message.author.id]
            
        last_senders = list(dict.fromkeys(reversed(last_senders)))
        if self.user.id in last_senders:
            last_senders.remove(self.user.id)

        self.last_senders[channel.id] = last_senders
        
        member2var = {str(member): '<P{}>'.format(m+1) for m, member in enumerate(last_senders)}
        member2var[str(self.user.id)] = '<S>'        
        var2member = {v: '<@!{}>'.format(k) for k, v in member2var.items()}
        
        # duplicate var2members in case vars > members
        num_vars = len(var2member)-1
        for v in range(num_vars+1, 25):
            var2member['<P{}>'.format(v)] = var2member['<P{}>'.format(1+(v-1)%num_vars)]
        
        self.member2var = member2var
        self.var2member = var2member

                
    async def run_program(self, program, message, channel=None, program_idx=0):
        if channel is None:
            channel = message.channel
        
        response, embed, file = '', None, None

        ##########################################
        ## GPT-3 chat
        ##########################################
        
        if program == 'gpt3_chat':
            response = await gpt3_chat.run(
                self.settings.programs.gpt3_chat, 
                message, 
                self.member2var, 
                self.var2member)
            
            
        ##########################################
        ## GPT-3 single prompt
        ##########################################

        elif program == 'gpt3_prompt':
            response = await gpt3_prompt.run(
                self.settings.programs.gpt3_prompt,
                message,
                program_idx)

            
        ##########################################
        ## ml4a
        ##########################################

        elif program == 'ml4a':  
            if message is not None:
                await channel.send('<@!{}> Drawing something, give me a few minutes...'.format(message.author.id))
            local_filename = ml4a_client.run(self.settings.programs.ml4a)
            file = discord.File(local_filename, filename=local_filename)
            if message is not None:
                response = '<@!{}>'.format(message.author.id)
         
        ##########################################
        ## Spotify                    
        ##########################################

        elif program == 'spotify':
            response, image_url = spotify.run(message, self.user.id)
            if image_url:
                embed = discord.Embed()
                embed.set_image(url=image_url)

        # truncate to Discord max character limit
        response = response[:2000]
        
        # send to discord
        await channel.send(response, embed=embed, file=file)


    async def add_reaction(self, message):
        last_message = re.sub('<@!?[0-9]+>', '', message.content)
        candidates = list(emoji_docs.keys())
        if last_message in emoji_search_results:
            result = emoji_search_results[last_message]
        else:
            result = gpt3.search(candidates, last_message, engine='curie')
            emoji_search_results[last_message] = result
        if 'data' not in result or len(result['data']) == 0:
            return
        scores = [doc['score'] for doc in result['data']]
        ranked_queries = list(reversed(np.argsort(scores)))
        ranked_candidates = [candidates[idx] for idx in ranked_queries]
        top_candidate = ranked_candidates[0]
        reaction = random.choice(emoji_docs[top_candidate]).strip()
        options = [{'candidate': candidates[idx], 'score': scores[idx]} 
                   for idx in ranked_queries
                   if scores[idx] > 20][:4]
        if len(options) == 0:
            return
        selected = random.choices([o['candidate'] for o in options], 
                                  weights=[o['score'] for o in options], k=1)[0]
        reaction = random.choice(emoji_docs[selected]).strip()
        await message.add_reaction(reaction)

    
    async def on_message(self, message):
        if not self.ready:
            return

        # lookup & replace tables from member id's to variables e.g. <P1>, <S>
        await self.update_member_lookup(message)

        # mentions and metadata
        all_mentions = re.findall('<@!?([0-9]+)>', message.content)
        mentioned = str(self.user.id) in all_mentions
        author_is_self = message.author.id == self.user.id
        
        # which context (on_message, on_mention, or background)
        behavior = self.settings.behaviors
        if mentioned:
            context = behavior.on_mention if 'on_mention' in behavior else None
        else:
            context = behavior.on_message if 'on_message' in behavior else None

        # maybe add a reaction to the message
        if context is not None \
        and not author_is_self \
        and 'reaction_probability' in context \
        and (random.random() < context.reaction_probability):
            await self.add_reaction(message)
             
        # skipping conditions
        channel_eligible = (message.channel.id in context.channels) if context and context.channels else True
        busy = len(self.timestamps) > 0
        decide_to_reply = False if not context else (random.random() < context.response_probability)

        # if any skipping conditions are True, stop
        if busy or author_is_self or not decide_to_reply or not channel_eligible:
            return
        
        # bot has decided to reply. add timestamp and delay
        delay = context.delay[0]+(context.delay[1]-context.delay[0])*random.random() if 'delay' in context else 0
        timestamp = {"time": time.time(), "delay": delay}
        self.timestamps.append(timestamp)

        # choose program based on search query, if specified
        options_search = None
        if 'options' in context and len(context.options):
            candidates = [opt['document'] for opt in context.options]
            query = re.sub('<@!?[0-9]+>', '', message.content)
            result = gpt3.search(candidates, query, engine='curie')
            scores = [doc['score'] for doc in result['data']]
            ranked_queries = list(reversed(np.argsort(scores)))
            options_search = [{'candidate': candidates[idx], 'score': scores[idx]} 
                              for idx in ranked_queries]
            for result in options_search[:2]:
                print(" -> %s : %0.2f" % (result['candidate'], result['score']))
            idx_top = ranked_queries[0]
            program = context.options[idx_top]['program']
            print("selected program:", program)
        
        else:
            program = context.program if 'program' in context else None
            if not program:
                print('No program selected')
                return
        
        # delay, run program, remove active timestamp
        await asyncio.sleep(timestamp['delay'])
        await self.run_program(program, message)
        self.timestamps.remove(timestamp)


    async def timed_event(self, event):
        channel = self.get_channel(event.channel)
        message = None
        program_index = event.program_index if 'program_index' in event else 0
        await self.run_program(event.program, 
                               message, 
                               channel, 
                               program_idx=program_index)
        
        
    async def schedule_timed_events(self):
        await self.wait_until_ready()
        
        if len(self.settings.behaviors.timed) == 0:
            return
        
        while True:
            now = datetime.now()
            timed_events = []
            for t in self.settings.behaviors.timed:
                if t.type == 'daily':
                    target_time = now.replace(hour=t.time[0], minute=t.time[1], second=0)
                elif t.type == 'sunrise':
                    latitude = float(os.getenv('LOCAL_LATITUDE'))
                    longitude = float(os.getenv('LOCAL_LONGITUDE'))
                    sunrise = Sun(latitude, longitude).get_sunrise_time()
                    sunrise = utc_to_local(sunrise).replace(tzinfo=None)
                    target_time = sunrise - timedelta(seconds=t.minutes_before * 60)
                elif t.type == 'sunset':
                    latitude = float(os.getenv('LOCAL_LATITUDE'))
                    longitude = float(os.getenv('LOCAL_LONGITUDE'))
                    sunset = Sun(latitude, longitude).get_sunset_time()
                    sunset = utc_to_local(sunset).replace(tzinfo=None)
                    target_time = sunset - timedelta(seconds=t.minutes_before * 60)
                while target_time < now:
                    target_time += timedelta(days=1)
                timed_events.append({'event': t, 'time': target_time})
            timed_events = sorted(timed_events, key=lambda k: k['time']) 
            next_event = timed_events[0]
            time_until = next_event['time'] - now
            print('time until next event: {}'.format(time_until))

            await asyncio.sleep(time_until.seconds)
            await self.timed_event(next_event['event'])
            await asyncio.sleep(90)
            
      
    async def background_process(self):
        await self.wait_until_ready()
        background = self.settings.behaviors.background        
        if not 'probability_trigger' in background or not 'every_num_minutes' in background:
            return
        prob_trigger = 1.0-math.pow(1.0-background.probability_trigger, 
                                    1.0/background.every_num_minutes)
        while not self.is_closed():

            if (random.random() < prob_trigger):
                channel = self.get_channel(background.channel)
                message = None
                program_index = background.program_index if 'program_index' in background else 0

                await self.run_program(background.program, 
                                       message, 
                                       channel, 
                                       program_idx=program_index)

            await asyncio.sleep(60) # run every 60 seconds


def main(): 
    load_dotenv()
    intents = discord.Intents.default()
    intents.members = True
    loop = asyncio.get_event_loop()
    for botname in botlist:
        client = DiscordBot(intents=intents)
        loop.create_task(client.setup(bots[botname]))
    loop.run_forever()


if __name__ == "__main__":
    main()