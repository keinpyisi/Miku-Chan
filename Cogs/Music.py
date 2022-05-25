from ntpath import join
import discord, wavelink, re, random, math, tempfile, json, os, shutil
from discord.ext import commands
from Cogs import Utils, Message, DisplayName, PickList, DL
import scrapetube
import random as r
# This file was originally based on Rapptz's basic_voice.py:
# https://github.com/Rapptz/discord.py/blob/master/examples/basic_voice.py

def setup(bot):
	settings = bot.get_cog("Settings")
	bot.add_cog(Music(bot,settings))

class Music(commands.Cog):

	__slots__ = ("bot","settings","ll_host","ll_port","ll_pass","NodePool","player_attrs","player_clear","vol_ratio","gc")

	def __init__(self, bot, settings):
		self.bot      = bot
		self.settings = settings
		# Get the Lavalink info as needed
		self.ll_host = bot.settings_dict.get("lavalink_host","lavalink.oops.wtf")
		self.ll_port = bot.settings_dict.get("lavalink_port",2000)
		self.ll_pass = bot.settings_dict.get("lavalink_password","www.freelavalink.ga")
		# Setup wavelink defaults
		self.NodePool = wavelink.NodePool()
		# Setup player specifics to remember
		self.player_attrs = ("skips","ctx","track_ctx","track_seek","repeat","vol","eq")
		self.player_clear = [x for x in self.player_attrs if not x in ("ctx","repeat","vol")] # Attributes to strip on start
		# Ratio to equalize volume
		self.vol_ratio = 0.75
		# Graphing char set - allows for theming-type overrides
		self.gc = bot.settings_dict.get("music_graph_chars",{})
		'''	"b"  :"│",  # "║" # Bar outline
			"tl" :"┌",  # "╔" # Top left outline
			"tr" : "┐", # "╗" # Top right outline
			"bl" : "└", # "╚" # Bottom left outline
			"br" : "┘", # "╝" # Bottom right oultine
			"cp" : "─", # "═" # Top/bottom cap
			"ep" : " ", #       Empty space
			"in" : " ", #       Inner fill char
			"se" : "─", # "═" # Netral Separator
			"su" : "┴", # "╩" # Separator up
			"sd" : "┬", # "╦" # Separator down
			"lp" : ""   #       Left pad'''
		global Utils, DisplayName
		Utils = self.bot.get_cog("Utils")
		DisplayName = self.bot.get_cog("DisplayName")

	async def get_node(self):
		# Try to get the best node - if any, otherwise create one
		try:
			return self.NodePool.get_node()
		except wavelink.ZeroConnectedNodes:
			return await self.NodePool.create_node(
				bot=self.bot,
				host=self.ll_host,
				port=self.ll_port,
				password=self.ll_pass,
			)

	async def get_player(self,guild):
		# Get (or create) a node, and return the guild's player (if any).
		# Returns either the player, or None if none exists.
		node = await self.get_node()
		return node.get_player(guild)

	def _is_submodule(self, parent, child):
		return parent == child or child.startswith(parent + ".")

	@commands.Cog.listener()
	async def on_unloaded_extension(self, ext):
		# Called to shut things down
		if not self._is_submodule(ext.__name__, self.__module__):
			return
		# Disconnect and cleanup all nodes and their current players
		nodes = list(self.NodePool.nodes.values())
		for node in nodes:
			# Disconnect all players, then disconnect the node
			for player in node.players:
				await self._stop(player,clear_attrs=True,clear_queue=True,disconnect=True)
			if node.is_connected():
				# Seems to cause a "Cannot write to closing transport" error, but it's
				# the suggested way in the docs... ¯\_(ツ)_/¯
				pass # await node.disconnect()

	@commands.Cog.listener()
	async def on_check_play(self,player):
		# Helper to see if we should play the next song.

		# Player either doesn't exist, or isn't connected - bail.
		if not player or not player.is_connected(): return
		# Player is already doing something or has nothing to do - bail.
		if player.is_playing() or player.is_paused(): return
		if player.queue.is_empty: return
		# Should be connected, and not already playing, let's strip any unwanted attributes
		# from our player
		self._clear_player(player)
		# Check for the "eq" attr - and if we don't have it, ensure we have a flat
		# eq setup.
		if not hasattr(player,"eq"):
			await self.reset_eq(player)
		if hasattr(player,"timescale"):
			# We just remove it as Lavalink doesn't retain it on new songs
			delattr(player,"timescale")
		# let's get our next track
		# and context and send the resulting message.
		track = await player.queue.get_wait()
		# Retain the local context and seek if needed
		if hasattr(track,"ctx"): player.track_ctx = track.ctx
		# We only want to pull the seek value from the current track.
		if hasattr(track,"seek"): player.track_seek = track.seek
		ctx = getattr(player,"ctx",getattr(player,"track_ctx",None))
		if ctx: # Got context, can send the resulting message
			delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
			fields = [{"name":"Duration","value":self.format_duration(track.duration,track),"inline":False}]
			if getattr(player,"track_seek",None): # Check both if we have the attr, and that it evaluates to true
				fields.append({"name":"Starting At","value":self.format_duration(player.track_seek),"inline":False})
			await Message.Embed(
				title="♫ Now Playing: {}".format(track.title),
				fields=fields,
				description="Requested by {}".format(ctx.author.mention),
				color=ctx.author,
				url=track.uri,
				thumbnail=getattr(track,"thumb",None),
				delete_after=delay
			).send(ctx)
		# Regardless of whether we can post - go to the next song.
		# Make sure volume is setup properly - equalized per the volume ratio
		volume = player.vol if hasattr(player,"vol") else self.settings.getServerStat(ctx.guild, "MusicVolume", 100) * self.vol_ratio
		if not player.volume == volume:
			await player.set_volume(volume)
			player.vol = player.volume # Retain the setting
		print("LINE 132 Music.py HERE IS TO PLAY VOICE")
		await player.play(track)
		if getattr(player,"track_seek",None): await player.seek(player.track_seek*1000)

	@commands.Cog.listener()
	async def on_wavelink_track_end(self,player,track,reason):
		await player.stop() # Stop the player - prevents issues with it thinking it's still playing
		# print("TRACK ENDED",player)
		# print(track)
		# print(reason)
		if getattr(player,"repeat",False):
			# We're repeating tracks - add it to the end
			if not hasattr(player,"track_ctx"): return # No context - probably stopped.
			track = self._track_fill(track, ctx=player.track_ctx)
			if hasattr(player,"track_seek"): track.seek = player.track_seek # Restore the seek position
			await self.add_to_queue(player,track)
		if player.queue.is_empty:
			# Nothing else to play - let's attempt to get our context.
			# If attached to the player, it's bound to the starting track - if we only have it on the
			# track, then we're just replying to the last queued element.
			ctx = getattr(player,"ctx",getattr(player,"track_ctx",None))
			color_ctx = getattr(player,"track_ctx",ctx)
			if not ctx: return # Nothing left to play, and nowhere to post our end message.
			delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
			return await Message.Embed(title="♫ End of playlist!",color=color_ctx.author,delete_after=delay).send(ctx)
		# Dispatch our check play command
		self.bot.dispatch("check_play",player)

	@commands.Cog.listener()
	async def on_wavelink_track_exception(self,player,track,error):
		await player.stop() # Stop the player - prevents issues with it thinking it's still playing
		print("TRACK EXCEPTION",player)
		print(track)
		print(error)

	@commands.Cog.listener()
	async def on_wavelink_track_stuck(self,player,track,threshold):
		await player.stop() # Stop the player - prevents issues with it thinking it's still playing
		print("TRACK STUCK",player)
		print(track)
		print(threshold)

	@commands.Cog.listener()
	async def on_voice_state_update(self, user, before, after):
		if not user.guild or not before.channel or (user.bot and user.id != self.bot.user.id):
			return # No guild, someone joined, or the user is a bot that's not us
		player = await self.get_player(before.channel.guild)
		if player is None:
			return # Player is borked or not connected - just bail
		if player.channel and player.channel != before.channel:
			return # No player to worry about, or someone left a different channel - ignore
		if user.id == self.bot.user.id and not after.channel:
			# We were disconnected somehow - try to reconnect and keep playing
			#
			##  Dirty workaround for what seems like a bug in wavelink  ##
			#
			#   If you manually disconnect the from vc via right click and then try to
			#   have it join a voice channel, it throws an exception stating that it's
			#   already connected to a voice channel.
			#   As a workaround - we manually update the voice state and reconnect to
			#   the prior channel.
			return await player.guild.change_voice_state(channel=before.channel)
		elif len([x for x in before.channel.members if not x.bot]) > 0:
			return # At least one non-bot user
		# if we made it here - then we're alone - disconnect and destroy
		await self._stop(player,clear_attrs=True,clear_queue=True,disconnect=True)

	def _clear_player(self, player, attrs=None):
		if attrs is True: # Clear all if True
			attrs = self.player_attrs
		elif not isinstance(attrs,(list,tuple)): # Clear standard if not a list/tuple
			attrs = self.player_clear
		for x in attrs:
			if hasattr(player,x):
				try: delattr(player,x)
				except: pass

	async def _check_role(self, ctx):
		# Checks if we have the required credentials to use the music player.
		if Utils.is_bot_admin(ctx):
			return True
		promoArray = self.settings.getServerStat(ctx.guild, "DJArray", [])
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not len(promoArray):
			await Message.Embed(title="♫ There are no DJ roles set yet.  Use `{}adddj [role]` to add some.".format(ctx.prefix),color=ctx.author,delete_after=delay).send(ctx)
			return None
		for role in promoArray:
			if ctx.guild.get_role(int(role["ID"])) in ctx.author.roles:
				return True
		await Message.Embed(title="♫ You need a DJ role to do that!",color=ctx.author,delete_after=delay).send(ctx)
		return False

	async def _stop(self, player, clear_attrs=True, clear_queue=True, disconnect=False):
		# Helper to stop the player and optionally clear queue and disconnect.
		if not player: return
		# First we clear custom attributes added to the player
		if clear_attrs:
			self._clear_player(player,attrs=self.player_attrs)
		# Then we clear the queue if needed
		if clear_queue and not player.queue.is_empty:
			player.queue.clear()
		# Then we stop if playing/paused
		if player.is_playing() or player.is_paused():
			await player.stop()
		# Finally we disconnect if needed
		if disconnect and player.is_connected():
			await player.disconnect()

	def get_tracks_from_data(self, data, check_start=True, check_seek=True, ctx=None):
		if not data or not isinstance(data,dict):
			# Something's wrong - return an empty list
			return []
		starting_track = 0 # Set the default
		if check_start: # We need to extract the starting point
			info = data.get("playlistInfo",{})
			# Get the starting track within limits
			starting_track = max(min(len(data["tracks"])-1, info.get("selectedTrack",0)), 0)
		new_tracks = []
		for x in data.get("tracks",[])[starting_track:]:
			new_track = self.get_track_with_info(x,check_seek=check_seek,ctx=ctx)
			if not new_track: continue # Skip if botched
			new_tracks.append(new_track)
		return new_tracks

	def get_track_with_info(self, info, check_seek=True, ctx=None):
		if isinstance(info,wavelink.abc.Playable): # Already a track - let's add needed info and return
			return self._track_fill(info,ctx)
		if not info or not isinstance(info,dict): return None # Not the right kind of info
		if all((x in info for x in ("track","info"))):
			# Should have the *actual* info dict now - create a track and return it
			new_track = wavelink.Track(id=info["track"],info=info["info"])
			if check_seek:
				if info["info"].get("seek"):
					new_track.seek = info["info"]["seek"] # Legacy method for old saves
				elif info["info"].get("position"):
					new_track.seek = info["info"]["position"]
		elif "id" in info: # It's a json track
			new_track = wavelink.Track(id=info["id"],info=info)
			if check_seek:
				if info.get("seek"):
					new_track.seek = info["seek"]
				elif info.get("position"):
					new_track.seek = info["position"]
		else: # Missing info - bail
			return None
		return self._track_fill(new_track,ctx)

	def _track_fill(self, track, ctx=None):
		# Helper to add ctx options to a track - as well as flesh out the thumbnail if needed
		if not isinstance(track,wavelink.abc.Playable): return track # Make no changes if it's the wrong type
		if hasattr(track,"info"): # Let's gather info and set things up if possible
			if track.info.get("sourceName") in ("youtube","ytmusic") and "identifier" in track.info:
				# We have the proper sourceName, and we have an identifier - let's build the thumb URL
				try:
					for x in ("thumb","thumbnail"):
						setattr(track,x,"https://img.youtube.com/vi/{}/maxresdefault.jpg".format(track.info["identifier"]))
				except: pass
			# Setup missing info from the dict as needed
			for x in ("id","identifier","uri","title","length","author"):
				if x in track.info:
					try: setattr(track,x,track.info[x])
					except: pass # Couldn't set it - probably already set
		if ctx: track.ctx = ctx # Append our ctx-based data if provided
		return track

	async def resolve_search(self, ctx, url, message = None, shuffle = False):
		# Helper method to search for songs/resolve urls and add the contents to the queue
		url = url.strip('<>')
		# Check if url - if not, remove /
		urls = Utils.get_urls(url)
		seek_pos = 0
		try:
			if urls: # Need to load via node get_tracks/get_playlist
				url = urls[0] # Get the first URL
				node = await self.get_node()
				if re.fullmatch(r"(?i).*(&|\?)list=.+",url): # Got a playlist
					tracks = await node.get_playlist(wavelink.abc.Playlist,identifier=url)
				else: # Probably a direct URL - try to load it
					tracks = await node.get_tracks(wavelink.Track,query=url)
					tracks = tracks[0] # Returns a list - get the first element
				# Let's also get the seek position if needed
				try:
					adj_dict = {"h":3600,"m":60,"s":1}
					seek_str = next((x[2:] for x in url.split("?")[1].split("&") if x.lower().startswith("t=")),"0").lower()
					values = [x for x in re.split("(\\d+)",seek_str) if x]
					# We should have a list of numbers and non-numbers.  Let's total the values
					total_time = 0
					last_type = "s" # Assume seconds in case no value is given
					for x in values[::-1]:
						if not x.isdigit():
							# Save the type
							last_type = x
							continue
						# We have a digit, let's calculate and add our time
						# Only factor hours, minutes, seconds - anything else is ignored
						total_time += int(x)*adj_dict.get(last_type,0) # Default to 0 if not a valid type
					seek_pos = total_time
				except Exception as e:
					seek_pos = 0
			else: # Got a search term - let's search
				if self.settings.getServerStat(ctx.guild, "YTMultiple", False):
					delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
					tracks = await wavelink.YouTubeTrack.search(query=url,return_first=False)
					# We want to let the user pick
					list_show = "Please select the number of the track you'd like to add:"
					index, message = await PickList.Picker(
						title=list_show,
						list=[x.info['title'] for x in tracks[:5]],
						ctx=ctx,
						message=message
					).pick()
					if index < 0:
						if index == -3: await message.edit(content="Something went wrong :(",delete_after=delay)
						elif index == -2: await message.edit(content="Times up!  We can search for music another time.",delete_after=delay)
						else: await message.edit(content="Aborting!  We can search for music another time.",delete_after=delay)
						return False
					# Got the index of the track to add
					tracks = tracks[index]
				else:
					# We only want the first entry
					tracks = await wavelink.YouTubeTrack.search(query=url,return_first=True)
				# TODO:  Setup multi-track display based on results per server settings
		except Exception as e:
			tracks = None # Clear it out as something went wrong
			print("Error resolving search:\n{}".format(repr(e)))
		# We need to figure out if we've loaded a playlist
		if hasattr(tracks,"info"):
			tracks = self.get_track_with_info(tracks,ctx=ctx)
			if seek_pos > 0: # Set the seek position
				tracks.seek = seek_pos
			# One track - let's just return it
			return {"tracks":tracks,"search":url}
		if hasattr(tracks,"data"):
			if not tracks.data.get("tracks"): return None # wut
			# Get the starting track within limits
			valid_tracks = self.get_tracks_from_data(tracks.data,check_start=True,ctx=ctx)
			if seek_pos > 0 and valid_tracks: # Set the seek position of the first track
				valid_tracks[0].seek = seek_pos
				if shuffle and len(valid_tracks)>1: # Shuffle the second on up
					shuffle_tracks = valid_tracks[1:]
					random.shuffle(shuffle_tracks)
					valid_tracks = [valid_tracks[0]]+shuffle_tracks
			elif shuffle: # Either no seek, or empty list - shuffle in place
				random.shuffle(valid_tracks)
			return {
				"data":tracks.data,
				"tracks":valid_tracks,
				"playlist":tracks.data.get("playlistInfo",{}).get("name","Unknown Playlist"),
				"search":url
			}
		return None

	def format_scale(self, player, prefix="", hide_100=True):
		# Returns a percent for the time scale
		timescale = getattr(player,"timescale",self.default_timescale())
		ts_perc = math.ceil(timescale.get("speed",1.0)/timescale.get("rate",1.0)*100)
		if hide_100 and ts_perc==100: return ""
		return "{}{:,}%".format(prefix,ts_perc)

	def apply_scale(self, player, time_value, reversed=False):
		if not isinstance(time_value,(int,float)): return time_value
		# Helper to get the current player's timescale and apply it to whatever passed time
		timescale = getattr(player,"timescale",self.default_timescale())
		if reversed: return time_value * timescale.get("speed",1.0) * timescale.get("rate",1.0)
		return time_value / timescale.get("speed",1.0) / timescale.get("rate",1.0)
	
	def format_duration(self, dur, track=None):
		if isinstance(track,wavelink.abc.Playable) and hasattr(track,"is_stream") and track.is_stream():
			# Might be a fleshed out wavelink.Track, might not.  We check as much as we can
			# to determine if it's a stream first.
			return "[Live Stream]"
		dur = int(dur)
		hours = int(dur//3600)
		minutes = int((dur%3600)//60)
		seconds = int(dur%60)
		return "{:02d}h:{:02d}m:{:02d}s".format(hours, minutes, seconds)

	def format_elapsed(self, player, track):
		progress = player.position # self.apply_scale(player,player.position)
		total    = self.apply_scale(player,track.duration)
		return "{} -- {}".format(self.format_duration(progress),self.format_duration(total,track))

	def progress_bar(self,player,track,bar_width=27,show_percent=True,include_time=False):
		# Returns a [#####-----] XX.x% style progress bar
		progress = player.position # self.apply_scale(player,player.position)
		total    = self.apply_scale(player,track.duration) if not hasattr(track,"is_stream") or not track.is_stream() else 0
		bar = ""
		# Account for the brackets
		bar_width = 10 if bar_width-2 < 10 else bar_width-2
		if total == 0:
			# We don't know how long the song is - or it's a stream
			# return a progress bar of [//////////////] instead
			bar = "[{}]".format("/"*bar_width)
		else:
			# Calculate the progress vs total
			p = int(round((progress/total*bar_width)))
			bar = "[{}{}]".format("■"*p,"□"*(bar_width-p))
		if show_percent:
			bar += " --%" if total == 0 else " {}%".format(int(round(progress/total*100)))
		if include_time:
			time_prefix = "{} - {}\n".format(self.format_duration(progress),self.format_duration(total,track))
			bar = time_prefix + bar
		return bar

	def progress_moon(self,player,track,moon_count=10,show_percent=True,include_time=False):
		# Make some shitty moon memes or something... thanks Midi <3
		progress = player.position # self.apply_scale(player,player.position)
		total    = self.apply_scale(player,track.duration) if not hasattr(track,"is_stream") or not track.is_stream() else 0
		if total == 0:
			# No idea how long this song is - let's make a repeating pattern
			# of moons - keeping this rotating moon code in, because it's kinda cool
			# moon_list = ["🌑","🌘","🌗","🌖","🌕","🌔","🌓","🌒"]*math.ceil(moon_count/8)
			moon_list = ["🌕","🌑"]*math.ceil(moon_count/2)
			moon_list = moon_list[:moon_count]
			bar = "".join(moon_list)
		else:
			# Each moon can be broken into 25% chunks
			moon_max = 100/moon_count
			percent  = progress/total*100
			full_moons = int(percent/moon_max)
			leftover   = percent%moon_max
			remaining  = int(leftover/(moon_max/4))
			bar = "🌕"*full_moons
			bar += ["🌑","🌘","🌗","🌖","🌕"][remaining]
			bar += "🌑"*(moon_count-full_moons-1)
		if show_percent:
			bar += " --%" if total == 0 else " {}%".format(int(round(progress/total*100)))
		if include_time:
			time_prefix = "{} - {}\n".format(self.format_duration(progress),self.format_duration(total,track))
			bar = time_prefix + bar
		return bar

	async def add_to_queue(self, player, song_list):
		# Add songs as long as they're applicable - returns number added
		added = 0
		if isinstance(song_list,list):
			for song in song_list:
				if isinstance(song,wavelink.abc.Playable):
					await player.queue.put_wait(song)
					added += 1
		elif isinstance(song_list,wavelink.abc.Playable):
			await player.queue.put_wait(song_list)
			added += 1
		return added

	async def state_added(self,ctx,songs,message=None,shuffled=False):
		# Helper to state songs added, whether or not they were shuffled - and to
		# edit a passed message (if any).
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not "tracks" in songs: return None # Missing info
		if isinstance(songs["tracks"],list): # Added a playlist
			embed = Message.Embed(
				title="♫ Added {}{} song{} from {}".format(
					len(songs["tracks"]),
					" shuffled" if shuffled else "",
					"" if len(songs["tracks"])==1 else "s",
					songs.get("playlist","Unknown Playlist")
				),
				description="Requested by {}".format(ctx.author.mention),
				url=songs.get("search"),
				delete_after=delay,
				color=ctx.author
			)
		elif isinstance(songs["tracks"],wavelink.abc.Playable): # Only added one track
			track = songs["tracks"]
			fields = [{"name":"Duration","value":self.format_duration(track.duration,track),"inline":False}]
			if getattr(track,"seek",None):
				fields.append({"name":"Starting At","value":self.format_duration(track.seek),"inline":False})
			embed = Message.Embed(
				title="♫ Enqueued: {}".format(track.title),
				description="Requested by {}".format(ctx.author.mention),
				fields=fields,
				color=ctx.author,
				thumbnail=getattr(track,"thumb",None),
				url=track.uri,
				delete_after=delay
			)
		else:
			return None # Wrong type :(
		# Edit if we need to - otherwise send a new message
		if message: return await embed.edit(ctx,message)
		return await embed.send(ctx)

	# TODO: Figure this out later...
	'''async def _check_player(self, player, ctx, delay=None, check_pause=True, respond=True):
		# Helper to verify the player
		player = await self.get_player(ctx.guild)
		if delay is None:
			delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not player or not player.is_connected():
			if respond:
				await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		elif not player.is_playing() and not (check_pause and player.is_paused()):
			if respond:
				await Message.Embed(title="♫ Not playing anything!",color=ctx.author,delete_after=delay).send(ctx)
		else:
			return True # Made it through the checks
		return False # Didn't make it..'''

	async def _load_playlist_from_url(self, url, ctx, shuffle = False):
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if url is None and len(ctx.message.attachments) == 0:
			return await ctx.send("Usage: `{}loadpl [url or attachment]`".format(ctx.prefix))
		if url is None:
			url = ctx.message.attachments[0].url
		message = await Message.Embed(title="♫ Downloading...",color=ctx.author).send(ctx)
		try:
			playlist = await DL.async_json(url.strip("<>"))
		except Exception as e:
			return await Message.Embed(title="♫ Couldn't serialize playlist!",description=str(e),color=ctx.author,delete_after=delay).edit(ctx,message)
		if not len(playlist): return await Message.Embed(title="♫ Playlist is empty!",color=ctx.author).edit(ctx,message)
		if not isinstance(playlist,list): return await Message.Embed(title="♫ Playlist json is incorrectly formatted!",color=ctx.author).edit(ctx,message)
		if shuffle: random.shuffle(playlist)
		# Let's walk the items and add them
		valid_tracks = self.get_tracks_from_data({"tracks":playlist},check_start=False,check_seek=True,ctx=ctx)
		await self.add_to_queue(player,valid_tracks)
		await Message.Embed(title="♫ Added {} {}song{} from playlist!".format(len(valid_tracks),"shuffled " if shuffle else "", "" if len(playlist) == 1 else "s"),color=ctx.author,delete_after=delay).edit(ctx,message)
		self.bot.dispatch("check_play",player)

	@commands.command()
	async def loadpl(self, ctx, *, url = None):
		"""Loads the passed playlist json data.  Accepts a url - or picks the first attachment.
		
		Note that the structure of this file is very specific and alterations may not work.
		
		Only files dumped via the savepl command are supported."""
		await self._load_playlist_from_url(url, ctx)

	@commands.command()
	async def shufflepl(self, ctx, *, url = None):
		"""Loads and shuffles the passed playlist json data.  Accepts a url - or picks the first attachment.
		
		Note that the structure of this file is very specific and alterations may not work.
		
		Only files dumped via the savepl command are supported."""
		await self._load_playlist_from_url(url, ctx, shuffle=True)

	@commands.command()
	async def savepl(self, ctx, *, options = ""):
		"""Saves the current playlist to a json file that can be loaded later.
		
		Note that the structure of this file is very specific and alterations may not work.
		
		Available options:

		ts : Exclude the timestamp of the currently playing song."""
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		# Get the options
		timestamp = True
		for x in options.split():
			if x.lower() == "ts":
				timestamp = False
		# Let's save the playlist
		current = player.track.info
		current["id"] = player.track.id
		queue = []
		for x in player.queue.copy():
			x.info["id"] = x.id
			queue.append(x.info)
		if current and (player.is_playing() or player.is_paused()):
			if timestamp: current["position"] = player.position
			queue.insert(0,current)
		if not len(queue):
			return await Message.Embed(title="♫ No playlist to save!",color=ctx.author,delete_after=delay).send(ctx)
		message = await Message.Embed(title="♫ Gathering info...",color=ctx.author).send(ctx)
		await Message.Embed(title="♫ Saving and uploading...",color=ctx.author).edit(ctx,message)
		temp = tempfile.mkdtemp()
		temp_json = os.path.join(temp,"playlist.json")
		try:
			json.dump(queue,open(temp_json,"w"),indent=2)
			await ctx.send(file=discord.File(temp_json))
		except Exception as e:
			return await Message.Embed(title="♫ An error occurred creating the playlist!",description=str(e),color=ctx.author).edit(ctx,message)
		finally:
			shutil.rmtree(temp,ignore_errors=True)
		return await Message.Embed(title="♫ Uploaded playlist!",color=ctx.author).edit(ctx,message)

	@commands.command(pass_context=True)
	async def searchlist(self, ctx, yes_no = None):
		"""Gets or sets whether or not the server will show a list of options when searching with the play command - or if it'll just pick the first (admin only)."""
		if not await Utils.is_admin_reply(ctx): return
		await ctx.send(Utils.yes_no_setting(ctx,"Music player search list","YTMultiple",yes_no))

	@commands.command(aliases=["summon"])
	async def join(self,ctx,*,channel=None):
		"""Joins a passed voice channel, or the author's if none passed."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if channel is None:
			if not ctx.author.voice:
				return await Message.Embed(title="♫ You need to pass a voice channel for me to join!",color=ctx.author,delete_after=delay).send(ctx)
			channel = ctx.author.voice.channel
		else:
			channel = DisplayName.channelForName(channel,ctx.guild,"voice")
		if not channel:
			return await Message.Embed(title="♫ I couldn't find that voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		player = await self.get_player(ctx.guild)
		if player and player.is_connected():
			if not (player.is_paused() or player.is_playing()):
				if player.channel != channel: # Only move if we need to
					await player.move_to(channel)
					return await Message.Embed(title="♫ Ready to play music in {}!".format(channel),color=ctx.author,delete_after=delay).send(ctx)
				else: # We're already there - whine
					return await Message.Embed(title="♫ I'm already in {}!".format(channel),color=ctx.author,delete_after=delay).send(ctx)
			else:
				return await Message.Embed(title="♫ I'm already playing music in {}!".format(player.channel),color=ctx.author,delete_after=delay).send(ctx)
		await channel.connect(cls=wavelink.Player)
		await Message.Embed(title="♫ Ready to play music in {}!".format(channel),color=ctx.author,delete_after=delay).send(ctx)
	def random_question(self):
			with open('suisei.json') as fp:
				data = json.load(fp)
				# questions = data[0]["videoId"]
				# print(questions)
				random_index = r.randint(0, len(data)-1)
				return data[random_index]['videoId']
	def random_miku(self):
			with open('miku.json') as fp:
				data = json.load(fp)
				# questions = data[0]["videoId"]
				# print(questions)
				random_index = r.randint(0, len(data)-1)
				return data[random_index]['videoId']

	@commands.command(aliases=["suisei"])
	async def suisui(self,ctx,*,channel=None):
		"""Joins a passed voice channel, or the author's if none passed."""
		
		await ctx.send('Suisei is Adding Songs for you. Please Wait until Suisei is Finish.')
		videos = scrapetube.get_channel("UC5zBLhF5HCmBrUHzT8g7wxQ")
		lst = list(videos)
		# the json file where the output must be stored
		out_file = open("suisei.json", "w")
		
		json.dump(lst, out_file, indent = 6)
		
		out_file.close()
		await self.play(ctx=ctx,url="https://www.youtube.com/watch?v="+self.random_question())
		for i in range(15):
			await self.play(ctx=ctx,url="https://www.youtube.com/watch?v="+self.random_question())

		
	@commands.command(aliases=["sui"])
	async def joinc(self,ctx,*, count = None):
			channel=None
			"""Joins a passed voice channel, or the author's if none passed."""
			
			await ctx.send('Suisei is Adding '+ count+' Songs for you. Please Wait until Suisei is Finish.')
			videos = scrapetube.get_channel("UC5zBLhF5HCmBrUHzT8g7wxQ")
			lst = list(videos)
			# the json file where the output must be stored
			out_file = open("suisei.json", "w")
			
			json.dump(lst, out_file, indent = 6)
			
			out_file.close()
			await self.play(ctx=ctx,url="https://www.youtube.com/watch?v="+self.random_question())
			print(int(count))
			for i in range(int(count)):
				await self.play(ctx=ctx,url="https://www.youtube.com/watch?v="+self.random_question())
		
	@commands.command(aliases=["miku"])
	async def joinf(self,ctx,*,channel=None):
		"""Joins a passed voice channel, or the author's if none passed."""
		
		await ctx.send('Suisei is Adding Miku Songs for you. Please Wait until Suisei is Finish.')
		videos = scrapetube.get_playlist("PL4wjQC0_Imx2q0RpN5u5NpeQrI64SfvNO")
		lst = list(videos)
		# the json file where the output must be stored
		out_file = open("miku.json", "w")
		
		json.dump(lst, out_file, indent = 6)
		
		out_file.close()
		
		
		await self.play(ctx=ctx,url="https://www.youtube.com/watch?v="+self.random_miku())
		for i in range(15):
			await self.play(ctx=ctx,url="https://www.youtube.com/watch?v="+self.random_miku())
	
	@commands.command(aliases=["mik"])
	async def miks(self,ctx,*, count = None):
		channel=None
		"""Joins a passed voice channel, or the author's if none passed."""
		
		await ctx.send('Suisei is Adding'+ count+' Miku Songs for you. Please Wait until Suisei is Finish.')
		videos = scrapetube.get_playlist("PL4wjQC0_Imx2q0RpN5u5NpeQrI64SfvNO")
		lst = list(videos)
		# the json file where the output must be stored
		out_file = open("miku.json", "w")
		
		json.dump(lst, out_file, indent = 6)
		
		out_file.close()
		await self.play(ctx=ctx,url="https://www.youtube.com/watch?v="+self.random_miku())
		for i in range(int(count)):
			await self.play(ctx=ctx,url="https://www.youtube.com/watch?v="+self.random_miku())

	@commands.command(hidden=True)
	async def okbye(self, ctx):
		"""Do you wanna build a snowman?"""

		await ctx.invoke(self.leave)

	@commands.command(aliases=["disconnect"])
	async def leave(self, ctx):
		"""Stops and disconnects the bot from voice."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if player and player.is_connected():
			await self._stop(player,clear_attrs=True,clear_queue=True,disconnect=True)
			return await Message.Embed(title="♫ I've left the voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)

	@commands.command(aliases=["p"])
	async def play(self, ctx, *, url = None):
		"""Plays from a url (almost anything youtube_dl supports) or resumes a currently paused song."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			await join(ctx)
			# await join(ctx)
		if player.is_paused() and url is None:
			# We're trying to resume
			await player.resume()
			return await Message.Embed(title="♫ Resumed: {}".format(player.track.title),color=ctx.author,delete_after=delay).send(ctx)
		if url is None:
			return await Message.Embed(title="♫ You need to pass a url or search term!",color=ctx.author,delete_after=delay).send(ctx)
		message = await Message.Embed(
			title="♫ Searching For: {}".format(url.strip("<>")),
			color=ctx.author
			).send(ctx)
		# Add our url to the queue
		songs = await self.resolve_search(ctx,url,message=message)
		# Take the songs we got back - if any - and add them to the queue
		if not songs or not "tracks" in songs: # Got nothing :(
			return await Message.Embed(title="♫ I couldn't find anything for that search!",description="Try using more specific search terms, or pass a url instead.",color=ctx.author,delete_after=delay).edit(ctx,message)

		await self.add_to_queue(player,songs["tracks"])
		await self.state_added(ctx,songs,message,shuffled=False)
		self.bot.dispatch("check_play",player) # Dispatch the event to check if we should start playing
	

	@commands.command()
	async def unplay(self, ctx, *, song_number = None):
		"""Removes the passed song number from the queue.  You must be the requestor, or an admin to remove it.  Does not include the currently playing song."""
		
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			await join(ctx)
			#await join(ctx)
		if player.queue.is_empty:
			# No songs in queue
			return await Message.Embed(title="♫ No songs in queue!", description="If you want to bypass a currently playing song, use `{}skip` instead.".format(ctx.prefix),color=ctx.author,delete_after=delay).send(ctx)
		try:
			song_number = int(song_number)-1
		except:
			return await Message.Embed(title="♫ Not a valid song number!",color=ctx.author,delete_after=delay).send(ctx)
		if song_number < 0 or song_number > len(player.queue):
			return await Message.Embed(title="♫ Out of bounds!  Song number must be between 2 and {}.".format(len(queue)),color=ctx.author,delete_after=delay).send(ctx)
		# Get the song at the index
		track = player.queue[song_number]
		if track.ctx.author == ctx.author or Utils.is_bot_admin(ctx):
			del player.queue[song_number]
			return await Message.Embed(title="♫ Removed {} at position {}!".format(track.title,song_number+1),color=ctx.author,delete_after=delay).send(ctx)
		await Message.Embed(title="♫ You can only remove songs you requested!", description="Only {} or an admin can remove that song!".format(track.ctx.author.mention),color=ctx.author,delete_after=delay).send(ctx)

	@commands.command(aliases=["unq"])
	async def unqueue(self, ctx, *, unqueue_from = None):
		"""Removes all songs you've added from the queue (does not include the currently playing song).
		Admins and bot-admins can pass a user to remove all of that user's queued songs, or 'all' to remove all songs from the queue."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			await join(ctx)
			#await join(ctx)
		if player.queue.is_empty:
			# No songs in queue
			return await Message.Embed(title="♫ No songs in queue!",description="If you want to bypass a currently playing song, use `{}skip` instead.".format(ctx.prefix),color=ctx.author,delete_after=delay).send(ctx)
		clear_from = ctx.author # Default to the author
		if unqueue_from: # We got something passed here - check for "all" first, as that has priority - and user next
			if unqueue_from.lower() in ("-all","all"):
				clear_from = True # Use "true" as a placeholder for everyone
			else: # Check if a user was passed
				check_user = DisplayName.memberForName(unqueue_from,ctx.guild)
				if check_user: # Got one - set it
					clear_from = check_user
		if Utils.is_bot_admin(ctx) or clear_from == ctx.author:
			queue = player.queue.copy()
			new_queue = [] if clear_from is True else [song for song in queue if song.ctx.author!=clear_from]
			removed = len(queue)-len(new_queue)
			if removed:
				player.queue.clear()
				if new_queue: await self.add_to_queue(player,new_queue)
				return await Message.Embed(title="♫ Removed {} song{} from queue!".format(removed,"" if removed == 1 else "s"),color=ctx.author,delete_after=delay).send(ctx)
			title = "♫ No songs in queue!" if clear_from is True else "♫ {} has no songs in the queue!".format(DisplayName.name(clear_from))
			return await Message.Embed(
				title=title,
				description="If you want to bypass a currently playing song, use `{}skip` instead.".format(ctx.prefix),
				color=ctx.author,
				delete_after=delay
			).send(ctx)
		await Message.Embed(title="♫ You can only remove songs you requested!", description="Only an admin or bot-admin can remove all queued songs or those requested by other users!",color=ctx.author,delete_after=delay).send(ctx)

	@commands.command()
	async def shuffle(self, ctx, *, url = None):
		"""Shuffles the current queue. If you pass a playlist url or search term, it first shuffles that, then adds it to the end of the queue."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			if url is None: # No need to connect to shuffle nothing
				await join(ctx)
				#eturn await Message.Embed(title="♫ I am not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
			if not ctx.author.voice:
				return await Message.Embed(title="♫ You are not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
			await player.connect(ctx.author.voice.channel.id)
		if url is None:
			if player.queue.is_empty:
				# No songs in queue
				return await Message.Embed(title="♫ No songs in queue!",color=ctx.author,delete_after=delay).send(ctx)
			# Get a list of current items, clear the existing queue, shuffle and add back
			queue = list(player.queue)
			random.shuffle(queue)
			player.queue.clear()
			await self.add_to_queue(player,queue)
			return await Message.Embed(title="♫ Shuffled {} song{}!".format(len(queue),"" if len(queue) == 1 else "s"),color=ctx.author,delete_after=delay).send(ctx)
		# We're adding a new song/playlist/search shuffled to the queue
		message = await Message.Embed(
			title="♫ Searching For: {}".format(url.strip("<>")),
			color=ctx.author
			).send(ctx)
		# Add our url to the queue
		songs = await self.resolve_search(ctx,url,message=message,shuffle=True)
		# Take the songs we got back - if any - and add them to the queue
		if not songs or not "tracks" in songs: # Got nothing :(
			return await Message.Embed(title="♫ I couldn't find anything for that search!",description="Try using more specific search terms, or pass a url instead.",color=ctx.author,delete_after=delay).edit(ctx,message)
		await self.add_to_queue(player,songs["tracks"])
		await self.state_added(ctx,songs,message,shuffled=True)
		self.bot.dispatch("check_play",player) # Dispatch the event to check if we should start playing

	@commands.command()
	async def pause(self, ctx):
		"""Pauses the currently playing song."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			await join(ctx)
			#return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if player.is_paused(): # Just toggle play
			return await ctx.invoke(self.play)
		if not player.is_playing():
			return await Message.Embed(title="♫ Not playing anything!",color=ctx.author,delete_after=delay).send(ctx)
		# Pause the track
		await player.pause()
		await Message.Embed(title="♫ Paused: {}".format(player.track.title),color=ctx.author,delete_after=delay).send(ctx)

	@commands.command()
	async def paused(self, ctx, *, moons = None):
		"""Lists whether or not the player is paused.  Synonym of the playing command."""
		
		await ctx.invoke(self.playing,moons=moons)

	@commands.command()
	async def resume(self, ctx):
		"""Resumes the song if paused."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			await join(ctx)
			#await join(ctx)
		if not player.is_paused():
			return await Message.Embed(title="♫ Not currently paused!",color=ctx.author,delete_after=delay).send(ctx)
		# We're trying to resume
		await player.resume()
		await Message.Embed(title="♫ Resumed: {}".format(player.track.title),color=ctx.author,delete_after=delay).send(ctx)

	@commands.command()
	async def seek(self, ctx, position = None):
		"""Seeks to the passed position in the song if possible.  Position should be in seconds or in HH:MM:SS format.  Prepend a + or - to seek relative to the current position."""

		if position is None or position.lower() in ["moon","moons","moonme","moon me"]: # Show the playing status
			return await ctx.invoke(self.playing,moons=position)
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			await join(ctx)
			#return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if not (player.is_playing() or player.is_paused()):
			return await Message.Embed(title="♫ Not playing anything!",color=ctx.author,delete_after=delay).send(ctx)
		# Try to resolve the position - first in seconds, then with the HH:MM:SS format
		relative = False
		positive = True
		current = player.position # Seconds
		if position.startswith(("-","+")):
			relative = True
			positive = position.startswith("+")
			position = position[1:]
		vals = position.split(":")
		seconds = 0
		multiplier = [3600,60,1]
		vals = ["0"] * (len(multiplier)-len(vals)) + vals if len(vals) < len(multiplier) else vals # Ensure we have 3 values
		for index,mult in enumerate(multiplier):
			try: seconds += mult * float("".join([x for x in vals[index] if x in "0123456789."])) # Try to avoid h, m, s suffixes
			except: return await Message.Embed(title="♫ Malformed seek value!",description="Please make sure the seek time is in seconds, or using HH:MM:SS format.",color=ctx.author,delete_after=delay).send(ctx)
		if relative:
			if not positive: seconds *= -1
			seconds += current
			if seconds < 0: seconds = 0
		await player.seek(seconds*1000)
		return await Message.Embed(title="♫ Seeking to {}!".format(self.format_duration(seconds)),color=ctx.author,delete_after=delay).send(ctx)

	@commands.command()
	async def playing(self, ctx, *, moons = None):
		"""Lists the currently playing song if any."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected() or not (player.is_playing() or player.is_paused()) or not player.track:
			# No client - and we're not playing or paused
			return await Message.Embed(
				title="♫ Currently Playing",
				color=ctx.author,
				description="Not playing anything.",
				delete_after=delay
			).send(ctx)
		play_text = "Playing" if (player.is_playing() and not player.is_paused()) else "Paused"
		track = player.track
		track_ctx = getattr(player,"track_ctx",None)
		cv = int(player.volume/self.vol_ratio)
		if not track_ctx:
			return await Message.Embed(
				title="Missing information!",
				description="Could not get the context for the currently playing song.",
				color=ctx.author,
				delete_after=delay
			).send(ctx)
		await Message.Embed(
			title="♫ Currently {}: {}".format(play_text,track.title),
			description="Requested by {}\n -- Volume at {}%{}".format(
				track_ctx.author.mention,
				cv,
				self.format_scale(player,prefix="\n -- Speed at ")
			),
			color=ctx.author,
			fields=[
				{"name":"Elapsed","value":self.format_elapsed(player,track),"inline":False},
				{"name":"Progress","value":self.progress_moon(player,track) if moons and moons.lower() in ["moon","moons","moonme","moon me"] else self.progress_bar(player,track),"inline":False}
			],
			url=track.uri,
			thumbnail=getattr(track,"thumb",None),
			delete_after=delay
		).send(ctx)

	@commands.command(aliases=["queue","q"])
	async def playlist(self, ctx):
		"""Lists the queued songs in the playlist."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected() or not (player.is_playing() or player.is_paused()):
			return await Message.Embed(
				title="♫ Current Playlist",
				color=ctx.author,
				description="Not playing anything.",
				delete_after=delay
			).send(ctx)
		play_text = "Playing" if player.is_playing else "Paused"
		track = player.track
		track_ctx = getattr(player,"track_ctx",None)
		if not track_ctx:
			return await Message.Embed(
				title="Missing information!",
				description="Could not get the context for the currently playing song.",
				color=ctx.author,
				delete_after=delay
			).send(ctx)
		fields = [{"name":"{}".format(track.title),"value":"Currently {} - at {} - Requested by {} - [Link]({})".format(
			play_text,
			self.format_elapsed(player,track),
			track_ctx.author.mention,
			track.uri),"inline":False},
		]
		if not player.queue.is_empty:
			total_time = 0
			total_streams = 0
			time_string = ""
			for x in player.queue:
				if x.duration: total_time+=x.duration-getattr(x,"seek",0)
				else: total_streams+=1
			if total_time:
				# Got time at least
				time_string += "{} total -- ".format(self.format_duration(total_time))
			if total_streams:
				# Got at least one stream
				time_string += "{:,} Stream{} -- ".format(total_streams, "" if total_streams == 1 else "s")
			q_text = "-- {:,} Song{} in Queue -- {}".format(len(player.queue), "" if len(player.queue) == 1 else "s", time_string)
			fields.append({"name":"♫ Up Next","value":q_text,"inline":False})
		for x,y in enumerate(player.queue,start=1):
			t_ctx = getattr(y,"ctx",None)
			fields.append({
				"name":"{}. {}".format(x,y.title),
				"value":"{}{} - Requested by {} - [Link]({})".format(
					self.format_duration(y.seek,y)+" -> " if hasattr(y,"seek") else "",
					self.format_duration(y.duration,y),
					t_ctx.author.mention if t_ctx else "Unknown",
					y.uri
				),
				"inline":False})
		
		pl_string = " - Repeat Enabled" if getattr(player,"repeat",False) else ""
		if len(fields) <= 11:
			await Message.Embed(
				title="♫ Current Playlist{}".format(pl_string),
				color=ctx.author,
				fields=fields,
				delete_after=delay,
				pm_after_fields=15
			).send(ctx)
		else:
			page,message = await PickList.PagePicker(title="♫ Current Playlist{}".format(pl_string),list=fields,timeout=60 if not delay else delay,ctx=ctx).pick()
			if delay:
				await message.delete()

	@commands.command()
	async def skip(self, ctx):
		"""Adds your vote to skip the current song.  50% or more of the non-bot users need to vote to skip a song.  Original requestors and admins can skip without voting."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		to_skip = False
		if not player or not player.is_connected():
			await join(ctx)
			#return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if not player.is_playing():
			return await Message.Embed(title="♫ Not playing anything!",color=ctx.author,delete_after=delay).send(ctx)
		# Check for added by first, then check admin
		if Utils.is_bot_admin(ctx):
			await Message.Embed(title="♫ Admin override activated - skipping!",color=ctx.author,delete_after=delay).send(ctx)
			to_skip = True
		elif hasattr(player,"track_ctx") and player.track_ctx.author == ctx.author:
			await Message.Embed(title="♫ Requestor chose to skip - skipping!",color=ctx.author,delete_after=delay).send(ctx)
			to_skip = True
		# At this point, we're not admin, and not the requestor, let's make sure we're in the same vc
		elif not ctx.author.voice or not ctx.author.voice.channel == player.channel:
			return await Message.Embed(title="♫ You have to be in the same voice channel as me to use that!",color=ctx.author,delete_after=delay).send(ctx)
		else:
			# Do the checking here to validate we can use this and etc.
			skips = getattr(player,"skips",[]) # Get the existing skips - or an empty list if none
			# Relsolve the skips
			new_skips = []
			if not player.channel:
				return await Message.Embed(title="♫ Something went wrong!",description="That voice channel doesn't seem to exist anymore...",color=ctx.author,delete_after=delay).send(ctx)
			for x in skips: # Walk the skips - removing bots and users no longer present
				member = ctx.guild.get_member(x)
				if not member or member.bot or not member in player.channel.members:
					continue
				# Got a valid user who's in the skip list and the voice channel
				new_skips.append(x)
			# Check if we're not already in the skip list
			if not ctx.author.id in new_skips:
				new_skips.append(ctx.author.id)
			# Let's get the number of valid skippers
			skippers = [x for x in player.channel.members if not x.bot]
			needed_skips = math.ceil(len(skippers)/2)
			if len(new_skips) >= needed_skips:
				# Got it!
				to_skip = True
				await Message.Embed(title="♫ Skip threshold met ({}/{}) - skipping!".format(len(new_skips),needed_skips),color=ctx.author,delete_after=delay).send(ctx)
			else:
				# Update the skips
				player.skips = new_skips
				await Message.Embed(title="♫ Skip threshold not met - {}/{} skip votes entered - need {} more!".format(len(new_skips),needed_skips,needed_skips-len(new_skips)),color=ctx.author,delete_after=delay).send(ctx)
		if to_skip:
			player.skips = [] # Reset the skips
			await player.stop() # Stop the current song

	@commands.command()
	async def unskip(self, ctx):
		"""Removes your vote to skip the current song."""
		
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			await join(ctx)
			#return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if not player.is_playing():
			return await Message.Embed(title="♫ Not playing anything!",color=ctx.author,delete_after=delay).send(ctx)
		
		skips = getattr(player,"skips",[])
		if not ctx.author.id in skips: return await Message.Embed(title="♫ You haven't voted to skip this song!",color=ctx.author,delete_after=delay).send(ctx)
		# We did vote - remove that
		skips.remove(ctx.author.id)
		player.skips = skips
		if not player.channel:
			return await Message.Embed(title="♫ Something went wrong!",description="That voice channel doesn't seem to exist anymore...",color=ctx.author,delete_after=delay).send(ctx)
		# Let's get the number of valid skippers
		skippers = [x for x in player.channel.members if not x.bot]
		needed_skips = math.ceil(len(skippers)/2)
		await Message.Embed(title="♫ You have removed your vote to skip - {}/{} votes entered - {} more needed to skip!".format(len(skips),needed_skips,needed_skips-len(skips)),color=ctx.author,delete_after=delay).send(ctx)

	@commands.command(aliases=["skipped"])
	async def skips(self, ctx, user = None):
		"""Lists the number of skips for the currently playing song."""
		
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		user = ctx.author if user is None else DisplayName.memberForName(user,ctx.guild)
		if user is None: return await Message.Embed(title="♫ I couldn't find that user!",color=ctx.author,delete_after=delay).send(ctx)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			await join(ctx)
			#return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if not player.is_playing():
			return await Message.Embed(title="♫ Not playing anything!",color=ctx.author,delete_after=delay).send(ctx)
		
		if not player.channel:
			return await Message.Embed(title="♫ Something went wrong!",description="That voice channel doesn't seem to exist anymore...",color=ctx.author,delete_after=delay).send(ctx)
		# Let's get the number of valid skippers
		skips = getattr(player,"skips",[])
		skippers = [x for x in player.channel.members if not x.bot]
		needed_skips = math.ceil(len(skippers)/2)
		# Now we build our embed
		await Message.Embed(
			title="♫ {}/{} votes - {} more needed to skip!".format(len(skips),needed_skips,needed_skips-len(skips)),
			description="{} **has{}** voted to skip this song.".format(user.mention,"" if user.id in skips else " not"),
			color=ctx.author,
			delete_after=delay
		).send(ctx)

	@commands.command()
	async def stop(self, ctx):
		"""Stops and empties the current playlist."""
		
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if player:
			if player.is_playing() or player.is_paused():
				player.queue.clear() # Empty the queue
				# Clear context to prevent end of playlist spam
				self._clear_player(player,[x for x in self.player_attrs if x != "repeat"]) # Clear everything but repeat
				await player.stop()
				return await Message.Embed(title="♫ Music stopped and playlist cleared!",color=ctx.author,delete_after=delay).send(ctx)
			else:
				return await Message.Embed(title="♫ Not playing anything!",color=ctx.author,delete_after=delay).send(ctx)
		await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)

	@commands.command()
	async def volume(self, ctx, volume = None):
		"""Changes the player's volume (0-150%)."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if player is None or not player.is_connected():
			await join(ctx)
			#return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if not player.is_playing() and not player.is_paused():
			return await Message.Embed(title="♫ Not playing anything!",color=ctx.author,delete_after=delay).send(ctx)
		if volume is None:
			# We're listing the current volume
			cv = int(player.volume/self.vol_ratio)
			return await Message.Embed(title="♫ Current volume at {}%.".format(cv),color=ctx.author,delete_after=delay).send(ctx)
		try: # Round volume up or down as needed
			volume = float(volume)
			volume = int(volume) if volume - int(volume) < 0.5 else int(volume)+1
		except:
			return await Message.Embed(title="♫ Volume must be an integer between 0-150.",color=ctx.author,delete_after=delay).send(ctx)
		# Ensure our volume is between 0 and 150
		volume = 150 if volume > 150 else 0 if volume < 0 else volume
		await player.set_volume(volume*self.vol_ratio)
		player.vol = player.volume
		# Save it to the server stats with range 10-100
		self.settings.setServerStat(ctx.guild, "MusicVolume", 10 if volume < 10 else 100 if volume > 100 else volume)
		await Message.Embed(title="♫ Changed volume to {}%.".format(volume),color=ctx.author,delete_after=delay).send(ctx)

	@commands.command()
	async def repeat(self, ctx, *, yes_no = None):
		"""Checks or sets whether to repeat the current playlist."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		player = await self.get_player(ctx.guild)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		current = getattr(player,"repeat",False)
		setting_name = "Repeat"
		if yes_no == None:
			msg = "{} currently {}!".format(setting_name,"enabled" if current else "disabled")
		elif yes_no.lower() in [ "yes", "on", "true", "enabled", "enable" ]:
			yes_no = True
			msg = '{} {} enabled!'.format(setting_name,"remains" if current else "is now")
		elif yes_no.lower() in [ "no", "off", "false", "disabled", "disable" ]:
			yes_no = False
			msg = '{} {} disabled!'.format(setting_name,"is now" if current else "remains")
		else:
			msg = "That's not a valid setting!"
			yes_no = current
		if yes_no is not None and yes_no != current:
			player.repeat = yes_no
		await Message.Embed(title="♫ "+msg,color=ctx.author,delete_after=delay).send(ctx)

	@commands.command()
	async def playingin(self, ctx):
		"""Shows the number of servers the bot is currently playing music in."""

		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		server_list = []
		nodes = list(self.NodePool.nodes.values())
		for node in nodes:
			for p in node.players:
				if p.is_playing() and not p.is_paused():
					server_list.append({
						"name":"{} ({}){}".format(
							p.guild.name,
							p.guild.id,
							" ({:,} more in queue)".format(len(p.queue)
							) if len(p.queue) else ""
						),
						"value":"{} - at {} - Requested by {} - [Link]({})".format(
							p.track.title,
							self.format_elapsed(p,p.track),
							p.track.ctx.author.mention,
							p.track.uri),
						"inline":False
					})
		msg = "♫ Playing music in {:,} of {:,} server{}.".format(len(server_list), len(self.bot.guilds), "" if len(self.bot.guilds) == 1 else "s")
		if server_list: await PickList.PagePicker(title=msg,list=server_list,ctx=ctx).pick()
		else: await Message.Embed(title=msg,color=ctx.author,delete_after=delay).send(ctx)

	@commands.command()
	async def stopall(self, ctx):
		"""Stops and disconnects the bot from all voice channels in all servers (owner-only)."""

		if not await Utils.is_owner_reply(ctx): return
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		players = 0
		for guild in self.bot.guilds:
			# Remove the per-server temp settings
			player = await self.get_player(guild)
			if player:
				players += 1
				await self._stop(player,clear_attrs=True,clear_queue=True,disconnect=True)
		await Message.Embed(title="♫ I've left all voice channels ({:,}/{:,})!".format(players,len(self.bot.guilds)),color=ctx.author,delete_after=delay).send(ctx)

	@commands.command()
	async def autodeleteafter(self, ctx, seconds = None):
		"""Lists or sets the current delay before auto-deleting music related messages (max of 300 seconds).  Set to an integer less than 10 to disable auto-deletion.  Requires bot-admin or admin to set."""
		
		if not Utils.is_bot_admin(ctx): seconds = None
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if seconds == None:
			# List the delay
			if delay == None:
				return await Message.Embed(title="♫ Music related messages are not auto-deleted!",color=ctx.author).send(ctx)
			else:
				return await Message.Embed(title="♫ Music related messages are auto-deleted after {} second{}!".format(delay, "" if delay == 1 else "s"),color=ctx.author,delete_after=delay).send(ctx)
		# Attempting to set it
		try:
			real = int(seconds)
		except:
			return await Message.Embed(title="♫ Seconds must be an integer!",color=ctx.author,delete_after=delay).send(ctx)
		if real < 10:
			self.settings.setServerStat(ctx.guild, "MusicDeleteDelay", None)
			return await Message.Embed(title="♫ Music related messages will not be auto-deleted!",color=ctx.author).send(ctx)
		real = 300 if real > 300 else real
		self.settings.setServerStat(ctx.guild, "MusicDeleteDelay", real)
		return await Message.Embed(title="♫ Music related messages will be auto-deleted after {} second{}!".format(real, "" if real == 1 else "s"),color=ctx.author,delete_after=real).send(ctx)

	@commands.command(pass_context=True)
	async def searchlist(self, ctx, yes_no = None):
		"""Gets or sets whether or not the server will show a list of options when searching with the play command - or if it'll just pick the first (admin only)."""
		if not await Utils.is_admin_reply(ctx): return
		await ctx.send(Utils.yes_no_setting(ctx,"Music player search list","YTMultiple",yes_no))

	@commands.command()
	async def disableplay(self, ctx, *, yes_no = None):
		"""Enables/Disables the music commands.  Helpful in case Youtube is rate limiting to avoid extra api calls and allow things to calm down.  Owners can still access music commands (owner only)."""
		
		if not await Utils.is_owner_reply(ctx): return
		await ctx.send(Utils.yes_no_setting(ctx,"Music player lock out","DisableMusic",yes_no,is_global=True))

	async def cog_before_invoke(self, ctx):
		# We don't need to ensure extra for the following commands:
		if ctx.command.name in ("playingin","autodeleteafter","disableplay","stopall","searchlist","playing","playlist"): return
		# General checks for all music player commands - with specifics filtered per command
		# If Youtube ratelimits - you can disable music globally so only owners can use it
		player = await self.get_player(ctx.guild)
		delay = self.settings.getServerStat(ctx.guild,"MusicDeleteDelay",20)
		if self.settings.getGlobalStat("DisableMusic",False) and not Utils.is_owner(ctx):
			# Music is off - and we're not an owner - disconnect if connected, then send the bad news :(
			if player: await player.disconnect()
			await Message.Embed(title="♫ Music player is currently disabled!",color=ctx.author,delete_after=delay).send(ctx)
			raise commands.CommandError("Music Cog: Music disabled.")
		# Music is enabled - let's make sure we have the right role
		if not await self._check_role(ctx):
			raise commands.CommandError("Music Cog: Missing DJ roles.")
		# If we're just using the join command - we don't need extra checks - they're done in the command itself
		if ctx.command.name in ("join",): return
		# We've got the role - let's join the author's channel if we're playing/shuffling and not connected
		if ctx.command.name in ("play","shuffle","loadpl","shufflepl") and ctx.author.voice:
			if not player: return await ctx.author.voice.channel.connect(cls=wavelink.Player)
		# Let's ensure the bot is connected to voice
		if not player or not player.is_connected():
			await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
			raise commands.CommandError("Music Cog: Not connected to a voice channel.")
		# Let's make sure the caller is connected to voice and the same channel as the bot - or a bot-admin
		if Utils.is_bot_admin(ctx): return # We good - have enough perms to override whatever
		if not ctx.author.voice or not player or not ctx.author.voice.channel == player.channel:
			await Message.Embed(title="♫ You have to be in the same voice channel as me to use that!",color=ctx.author,delete_after=delay).send(ctx)
			raise commands.CommandError("Music Cog: Author not connected to the bot's voice channel.")


	###
	### HORRIBLY cursed filter functions hacked together for reasons.
	### Hidden below everything else because cursed, ofc.
	###

	async def apply_filters(self,player,filter_json,name=None):
		# Helper to apply filters to the passed player
		# Here, we basically just rip the functionality directly from Wavelink's
		# player class - whenever it sends info to the websocked.  We just imitate
		# that, but using our own filter data.
		if not filter_json: # Got... nothing
			return
		if name: # Wrap it in a dict
			filter_json = {name:filter_json}
		# Build a payload with the filters
		filter_json["op"] = "filters"
		filter_json["guildId"] = str(player.guild.id)
		# Send the payload to the websocket and pray
		await player.node._websocket.send(**filter_json)

	def _draw_band(self, band, max_len=5):
		v = max(min(float(band),1.),-1.) # Get the value as a float and force -1 to 1 range
		ep = self.gc.get("ep"," ")
		se = self.gc.get("se","─")
		ab = math.ceil(abs(v)*max_len)
		l = self.gc.get("tl","┌") if v>0 else self.gc.get("bl","└")
		r = self.gc.get("tr","┐") if v>0 else self.gc.get("bl","┘")
		s = self.gc.get("su","┴") if v>0 else self.gc.get("sd","┬")
		c = self.gc.get("cp","─")
		i = self.gc.get("in"," ")
		b = self.gc.get("b","│")
		# Iterate and draw
		return [
			# Draws top->down; Empty, Cap, Fill, Sep, Fill, Cap, Empty
			"{}{}{}{}{}{}{}".format(
				ep*(max_len-(ab if v>0 else 0)),
				t[0]*(1 if v>0 else 0),
				t[1]*(ab-1 if v>0 else 0),
				t[2] if v!=0 else se,
				t[1]*(ab-1 if v<0 else 0),
				t[0]*(1 if v<0 else 0),
				ep*(max_len-(ab if v<0 else 0))
			)
			for t in ((l,b,s),(c,i,se),(r,b,s))
		]

	def print_eq(self, eq, max_len=5):
		# EQ values are from -0.25 (muted) to 0.25 (doubled)
		eq_list  = []
		vals = ""
		se = self.gc.get("se","─")
		lp = self.gc.get("lp","")
		for entry in eq:
			value = entry.get("gain",.0)*4. # Quadrupled for -1 to 1 range
			eq_list.extend(self._draw_band(value,max_len=max_len))
			vals += str(math.ceil(abs(value)*max_len)*(1 if value>0 else -1)).rjust(2)+" "
		# Rotate the eq 90 degrees
		graph = "```\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n```".format(
			"Bands".center(len(vals),se),
			" ".join([str(x+1).rjust(2) for x in range(len(eq))]),
			se*(len(vals)),
			"\n".join([lp+x+lp for x in map("".join, zip(*eq_list))]),
			"Values".center(len(vals),se),
			vals,
			se*(len(vals))
		)
		return graph

	##
	## Presets taken from the older Wavelink repo:
	## https://github.com/PythonistaGuild/Wavelink/blob/3e11c16516dd89791c1247032045385979736554/wavelink/eqs.py#L82-L128
	##

	def flat_eq(self):
		# Function to return an initialized eq; 15 numbered bands of 0 gain
		return [{"band":x,"gain":0.0} for x in range(15)]

	def boost_eq(self):
		return [
			{"band": 0, "gain": -0.075},
			{"band": 1, "gain": 0.125},
			{"band": 2, "gain": 0.125},
			{"band": 3, "gain": 0.1},
			{"band": 4, "gain": 0.1},
			{"band": 5, "gain": 0.05},
			{"band": 6, "gain": 0.075},
			{"band": 7, "gain": 0.0},
			{"band": 8, "gain": 0.0},
			{"band": 9, "gain": 0.0},
			{"band": 10, "gain": 0.0},
			{"band": 11, "gain": 0.0},
			{"band": 12, "gain": 0.125},
			{"band": 13, "gain": 0.15},
			{"band": 14, "gain": 0.05}
		]

	def metal_eq(self):
		return [
			{"band": 0, "gain": 0.0},
			{"band": 1, "gain": 0.1},
			{"band": 2, "gain": 0.1},
			{"band": 3, "gain": 0.15},
			{"band": 4, "gain": 0.13},
			{"band": 5, "gain": 0.1},
			{"band": 6, "gain": 0.0},
			{"band": 7, "gain": 0.125},
			{"band": 8, "gain": 0.175},
			{"band": 9, "gain": 0.175},
			{"band": 10, "gain": 0.125},
			{"band": 11, "gain": 0.125},
			{"band": 12, "gain": 0.1},
			{"band": 13, "gain": 0.075},
			{"band": 14, "gain": 0.0}
		]

	def piano_eq(self):
		return [
			{"band": 0, "gain": -0.25},
			{"band": 1, "gain": -0.25},
			{"band": 2, "gain": -0.125},
			{"band": 3, "gain": 0.0},
			{"band": 4, "gain": 0.25},
			{"band": 5, "gain": 0.25},
			{"band": 6, "gain": 0.0},
			{"band": 7, "gain": -0.25},
			{"band": 8, "gain": -0.25},
			{"band": 9, "gain": 0.0},
			{"band": 10, "gain": 0.0},
			{"band": 11, "gain": 0.25},
			{"band": 12, "gain": 0.25},
			{"band": 13, "gain": -0.025}
		]

	async def reset_eq(self, player):
		# Remove the attribute and set a flat eq
		if hasattr(player,"eq"):
			delattr(player,"eq")
		await self.apply_filters(player,self.flat_eq(),name="equalizer")

	def default_timescale(self):
		return {
			"speed":1.0,
			"pitch":1.0,
			"rate":1.0
		}

	def print_timescale(self,timescale):
		return "```\nSpeed: {}\nPitch: {}\nRate:  {}\n```".format(
			timescale.get("speed","Unknown"),
			timescale.get("pitch","Unknown"),
			timescale.get("rate","Unknown")
		)

	'''@commands.command(aliases=["ts","tscale"])
	async def timescale(self, ctx, *, speed = None, pitch = None, rate = None):
		"""Gets or sets the current player's time scale values.  Speed, pitch, and rate can be any number between 0 and 10 with 1 being default."""
		# All values just get dumped into speed
		player = await self.get_player(ctx.guild)
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		ts = getattr(player,"timescale",self.default_timescale())
		if speed is None: # Print the current settings
			return await Message.Embed(title="♫ Current Timescale Settings",description=self.print_timescale(ts),color=ctx.author,delete_after=delay).send(ctx)
		# We got... something.  Let's try to parse it
		try:
			for i,x in enumerate(speed.replace(",","").lower().split()):
				if "=" in x: # Let's see if we are setting a specific value
					y,x = x.split("=")
					i = ["s","p","r"].index(y[0].lower())
				x = float(x)
				ts[list(ts)[i]]=x
		except:
			return await Message.Embed(title="♫ Invalid Timescale Value",description="Valid speed, pitch, and rate values are numbers from 0 to 10 with 1 being default.",color=ctx.author,delete_after=delay).send(ctx)
		await self.apply_filters(player,ts,name="timescale")
		player.timescale = ts # Set the player's timescale value
		return await Message.Embed(
			title="♫ Timescale Settings Updated!",
			description=self.print_timescale(ts),
			color=ctx.author,
			delete_after=delay,
			footer="Filter changes may take a bit to apply"
		).send(ctx)

	@commands.command(aliases=["rts","resetts"])
	async def resettimescale(self, ctx):
		"""Resets the current player's time scale values."""

		player = await self.get_player(ctx.guild)
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		ts = self.default_timescale()
		await self.apply_filters(player,ts,name="timescale")
		player.timescale = ts # Set the player's timescale value
		return await Message.Embed(
			title="♫ Timescale Settings Updated!",
			description=self.print_timescale(ts),
			color=ctx.author,
			delete_after=delay,
			footer="Filter changes may take a bit to apply"
		).send(ctx)'''

	@commands.command(aliases=["eq"])
	async def geteq(self, ctx):
		"""Prints the current equalizer settings."""

		player = await self.get_player(ctx.guild)
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		# Get the current eq
		eq = getattr(player,"eq",self.flat_eq())
		return await Message.Embed(title="♫ Current Equalizer Settings",description=self.print_eq(eq),color=ctx.author,delete_after=delay).send(ctx)
	
	@commands.command(aliases=["seq"])
	async def seteq(self, ctx, *, bands = None):
		"""Sets the equalizer to the passed 15 space-delimited values from -5 (silent) to 5 (double volume)."""
		
		player = await self.get_player(ctx.guild)
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if bands is None: 
			return await Message.Embed(title="♫ Please specify the eq values!",description="15 numbers separated by a space from -5 (silent) to 5 (double volume)",color=ctx.author,delete_after=delay).send(ctx)
		try:
			band_ints = [int(x) for x in bands.split()]
		except:
			return await Message.Embed(title="♫ Invalid eq values passed!",description="15 numbers separated by a space from -5 (silent) to 5 (double volume)",color=ctx.author,delete_after=delay).send(ctx)
		if not len(band_ints) == 15: return await Message.Embed(title="♫ Incorrect number of eq values! ({:,} - need 15)".format(len(band_ints)),description="15 numbers separated by a space from -5 (silent) to 5 (double volume)",color=ctx.author,delete_after=delay).send(ctx)
		eq = [{"band":x,"gain":float(0.25 if y/20 > 0.25 else -0.25 if y/20 < -0.25 else y/20)} for x,y in enumerate(band_ints)]
		await self.apply_filters(player,eq,name="equalizer")
		player.eq = eq # Set the player's eq value to the list
		# self.settings.setServerStat(ctx.guild, "MusicEqualizer", player.eq.raw)
		return await Message.Embed(
			title="♫ Set equalizer to Custom preset!",
			description=self.print_eq(eq),
			color=ctx.author,
			delete_after=delay,
			footer="Filter changes may take a bit to apply"
		).send(ctx)

	@commands.command(aliases=["sb"])
	async def setband(self, ctx, band_number = None, value = None):
		"""Sets the value of the passed eq band (1-15) to the passed value from -5 (silent) to 5 (double volume)."""

		player = await self.get_player(ctx.guild)
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if band_number is None or value is None:
			return await Message.Embed(title="♫ Please specify a band and value!",description="Bands can be between 1 and 15, and eq values from -5 (silent) to 5 (double volume)",color=ctx.author,delete_after=delay).send(ctx)
		try:
			band_number = int(band_number)
			assert 0 < band_number < 16
		except:
			return await Message.Embed(title="♫ Invalid band passed!",description="Bands can be between 1 and 15, and eq values from -5 (silent) to 5 (double volume)",color=ctx.author,delete_after=delay).send(ctx)
		try:
			value = int(value)
			value = -5 if value < -5 else 5 if value > 5 else value
		except:
			return await Message.Embed(title="♫ Invalid eq value passed!",description="Bands can be between 1 and 15, and eq values from -5 (silent) to 5 (double volume)",color=ctx.author,delete_after=delay).send(ctx)
		eq = getattr(player,"eq",self.flat_eq())
		eq[band_number-1]["gain"] = float(value/20)
		await self.apply_filters(player,eq,name="equalizer")
		player.eq = eq
		# self.settings.setServerStat(ctx.guild, "MusicEqualizer", player.eq.raw)
		return await Message.Embed(
			title="♫ Set band {} to {}!".format(band_number,value),
			description=self.print_eq(eq),
			color=ctx.author,
			delete_after=delay,
			footer="Filter changes may take a bit to apply"
		).send(ctx)

	@commands.command(aliases=["req"])
	async def reseteq(self, ctx):
		"""Resets the current eq to the flat preset."""

		player = await self.get_player(ctx.guild)
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		
		eq = self.flat_eq()
		await self.apply_filters(player,eq,name="equalizer")
		player.eq = eq
		return await Message.Embed(
			title="♫ Reset equalizer!",
			description=self.print_eq(eq),
			color=ctx.author,
			delete_after=delay,
			footer="Filter changes may take a bit to apply"
		).send(ctx)

	@commands.command(aliases=["eqp"])
	async def eqpreset(self, ctx, preset = None):
		"""Sets the current eq to one of the following presets:  Boost, Flat, Metal"""

		player = await self.get_player(ctx.guild)
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if preset is None or not preset.lower() in ("boost","flat","metal","piano"):
			return await Message.Embed(title="♫ Please specify a valid eq preset!",description="Options are:  Boost, Flat, Metal, Piano",color=ctx.author,delete_after=delay).send(ctx)
		preset = preset.lower()
		eq = self.flat_eq() if preset=="flat" else self.boost_eq() if preset=="boost" else self.metal_eq() if preset=="metal" else self.piano_eq()
		await self.apply_filters(player,eq,name="equalizer")
		player.eq = eq
		return await Message.Embed(
			title="♫ Set equalizer to {} preset!".format(preset.capitalize()),
			description=self.print_eq(eq),
			color=ctx.author,
			delete_after=delay,
			footer="Filter changes may take a bit to apply"
		).send(ctx)

	@commands.command(aliases=["freq"])
	async def setfreq(self, ctx, freq_family=None, value=None):
		"""Sets the frequency family to the passed value.  Valid families are bass, mid, treble."""

		player = await self.get_player(ctx.guild)
		delay = self.settings.getServerStat(ctx.guild, "MusicDeleteDelay", 20)
		if not player or not player.is_connected():
			return await Message.Embed(title="♫ Not connected to a voice channel!",color=ctx.author,delete_after=delay).send(ctx)
		if not freq_family or not freq_family[0].lower() in ("b","l","m","t","h"): # Bass/Low, Middle, High/Treble
			return await Message.Embed(title="♫ Please specify a valid frequency family!",description="Options are:  Bass, Mid, Treble",color=ctx.author,delete_after=delay).send(ctx)
		# Normalize to the first letter - and change h->t and l->b
		freq_family = freq_family[0].lower()
		if freq_family in ("h","l"): freq_family = "t" if freq_family=="h" else "b"
		try:
			value = int(value)
			value = -5 if value < -5 else 5 if value > 5 else value
		except:
			return await Message.Embed(title="♫ Invalid value passed!",description="Frequency family eq values can be between -5 (silent) to 5 (double volume)",color=ctx.author,delete_after=delay).send(ctx)
		# Get the current eq
		eq = getattr(player,"eq",self.flat_eq())
		# [0  1  2  3  4]  5  6  7  8  9  [10 11 12 13 14]
		merge_values = (0,4) if freq_family=="b" else (5,9) if freq_family=="m" else (10,14)
		for entry in eq:
			if merge_values[0]<=entry["band"]<=merge_values[1]:
				# Adjust
				entry["gain"] = float(value/20)
			elif merge_values[0]-entry["band"]==1 or entry["band"]-merge_values[1]==1:
				# Next nearest - go half
				entry["gain"] = (entry["gain"]+float(value/20))/2
		await self.apply_filters(player,eq,name="equalizer")
		player.eq = eq
		f_name = {"b":"Bass","m":"Mid Range","t":"Treble"}
		return await Message.Embed(
			title="♫ Set {} to {}!".format(f_name.get(freq_family,"Unknown Family"),value),
			description=self.print_eq(eq),
			color=ctx.author,
			delete_after=delay,
			footer="Filter changes may take a bit to apply"
		).send(ctx)

	

		
		
