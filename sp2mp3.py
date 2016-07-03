import lxml.html as lh
from io import StringIO
import urllib2
import argparse
import re
import json
import requests
import base64
from collections import OrderedDict
import time
import string
from difflib import SequenceMatcher
import numpy as np

from os import listdir
from os.path import isfile, join

from apiclient.discovery import build
from apiclient.errors import HttpError
from oauth2client.tools import argparser

from tqdm import tqdm
from IPython import embed

DEVELOPER_KEY = "your-developer-key"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

class Provider:
	def __init__(self):
		pass

	def get_download_url(self, info):
		pass

	def get_mp3(self, url, unique_id):
		pass 

class Mukke(Provider):
	def encode_download_url(self, song):
		d = OrderedDict()
		d['title'] = song['title']
		d['engine'] = 'VKontakte'
		if song['uri']:
			d['uri'] = song['uri']
		d['id'] = song['id']

		json_data = json.dumps(d)
		json_data = json_data.replace('" ', '"')
		json_data = json_data.replace(' "', '"')
		url = base64.b64encode(json_data)
		url = url.replace('=', '!')
		url = url.replace('+', '-')
		url = url.replace('/', '_')
		return url

	def evaluate_search_results(self, results, query, duration):
		def str_to_secs(s):
			return int(s.split(':')[0])*60 + int(s.split(':')[1])

		titles    = [d['title'] for d in results]
		durations = [d['duration'] for d in results]
		
		ratios 		= np.array([SequenceMatcher(None, query, l).ratio() for l in titles])
		sec_diff 	= np.array([abs(str_to_secs(d) - str_to_secs(duration)) for d in durations]).astype(np.float32)
		sec_diff 	= sec_diff / np.max(sec_diff)
		corrected_ratios = ratios - sec_diff
		max_index 	= np.argmax(corrected_ratios)

		return results[max_index]

	def query_mukke(self, query):
		data = json.dumps({
			's': query,
			'engine': 'VKontakte',
			'page': 1
		})
		headers = {
			'Content-Type': 'application/json; charset=utf-8', 
			'X-Requested-With': 'XMLHttpRequest'
		}

		return requests.post('http://mukke.in/api/search/', data=data, headers=headers)

	def get_download_url(self, info):
		for query in ['%s %s' % (info['artist'], info['title']), info['title']]:
			res = self.query_mukke(query)
			if res.status_code == 200:
				json_response = json.load(StringIO(unicode(res.content)))	
				if 'songs' in json_response and len(json_response['songs']):
					best_result = self.evaluate_search_results(results=json_response['songs'], query=query, duration=info['duration'])
					return 'http://mukke.in/api/download/%s' % self.encode_download_url(best_result)
			elif res.status_code == 500:
				time.sleep(5*60)
				return self.get_download_url(info)

		return None

	def get_mp3(self, url, unique_id):
		r = requests.get(url, stream=True, timeout=3)

		# Extract filename from download file
		try:
			f_header = re.findall('filename="(.+)"', r.headers['Content-Disposition'])[0]
		except KeyError as e:
			embed()
		f_header = filter(lambda x: x in set(string.printable), f_header)

		# Replace illegal characters from filename
		for c in '/|?':
			f_header = f_header.replace(c, '')
		local_file = 'output/%s. %s' % (unique_id, f_header)

		# Save to disk
		with open(local_file, 'wb') as f:
			for chunk in r.iter_content(chunk_size=1024):
				if chunk:
					f.write(chunk)

class Youtube(Provider):
	def get_yt_videoid(self, info):
		youtube = build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, developerKey=DEVELOPER_KEY)

		res = youtube.search().list(
			q='%s %s' % (info['title'], info['artist']),
			part='id',
			type='video',
			maxResults=1
		).execute()

		return res['items'][0]['id']['videoId'] if len(res['items']) else None

	def get_download_url(self, info):
		return 'http://youtube.com/watch?v=%s' % get_yt_videoid(info)

class Spotify(object):
	def __init__(self, options, provider=None):
		assert(issubclass(provider.__class__, Provider))

		print 'Downloading via %s' % provider.__class__.__name__

		self.provider = provider
		self.options = options

	def read_output_file(self, f_out):
		with open(f_out, 'r') as out_file:
			lines = out_file.read().split()

		return lines

	def parse_spotify_page(self, url):
		song_info = {}

		# Fetch page of song
		req = urllib2.Request(url)
		try:
			# Parse HTML
			root = lh.parse(urllib2.urlopen(req))

			# Title
			title  = root.xpath('//div[@class="media-bd"]/h1/text()')
			title  = title[0] if title else None
			title  = title.split(' - ')[0] if ' - ' in title else title # Remove `title` - <more stuff>
			p_title = re.compile('( \(.+\))')
			title  = re.split(p_title, title)[0] if re.search(p_title, title) else title # Remove redundant information between brackets

			# Artist
			artist = root.xpath('//div[@class="media-bd"]/h2/a/text()')
			artist = artist if artist else None
			artist = artist[0] if type(artist) == list else artist
			
			duration = root.xpath('//div[contains(@class, "entity-additional-info")]/text()')
			p_duration = re.compile('(\d+:\d+)')
			duration = re.search(p_duration, duration[0]).group(1)

			song_info = {
				'title': title,
				'artist': artist,
				'duration': duration
			}
		except urllib2.URLError as e:
			print e.reason()
		
		return song_info

	def process(self):
		# Read input file
		with open(self.options.in_file, 'r') as in_file:
			urls = in_file.read().split()

		# Read output file to resume where we left of
		num_skip = 0
		if isfile(self.options.out_file):
			lines_out = self.read_output_file(self.options.out_file)
			num_skip = len(lines_out)

		for i, url in tqdm(enumerate(urls), total=len(urls)):
			if i + 1 <= num_skip:
				continue

			# Check if each input url is the one we are looking for
			p = re.compile('https\://open\.spotify\.com/track/[a-zA-Z0-9]{22}')
			m = re.match(p, url)
			if m:
				info = self.parse_spotify_page(url)
				download_url = self.provider.get_download_url(info)
				with open(self.options.out_file, 'a') as out_file:
					output = '%s\n' % download_url if download_url else '\n'
					out_file.write(output)
			else:
				raise Exception('Input file contains an invalid input at line %d' % i)

	def download(self):
		# Check for files that are already downloaded
		path = 'output'
		p = re.compile('(\d+)\.')
		song_ids = [int(re.search(p, f).group(1)) for f in listdir(path) if isfile(join(path, f))]

		lines_out = self.read_output_file(self.options.out_file)
		for i, line in tqdm(enumerate(lines_out), total=len(lines_out)):
			if i + 1 in song_ids:
				continue

			try:
				self.provider.get_mp3(line, unique_id=i + 1)
			except requests.exceptions.ReadTimeout as e:
				continue
			except requests.exceptions.ConnectTimeout as e:
				continue


def main(args):
	# Choose correct provider
	provider = None
	if args.provider == 'youtube':
		provider = Youtube()
	elif args.provider == 'mukke':
		provider = Mukke()
	else:
		raise Exception('Unkown provider: "%s"!' % args.provider)

	sp = Spotify(provider=provider, options=args)
	#sp.process()
	sp.download()

if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Fetch YouTube mp3s from a list of Spotify songs')
	parser.add_argument('in_file', help='file containing Spotify track URLs')
	parser.add_argument('out_file', help='output file')
	parser.add_argument('--provider', help='download provider', choices=['youtube', 'mukke'])

	args = parser.parse_args()

	main(args)