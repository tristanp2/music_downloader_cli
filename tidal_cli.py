from build_cookies_for_tidal import buildtidalcookies
from datetime import datetime
from musicdl import musicdl
import os
import json

playlist_url = 'https://tidal.com/playlist/dd476c79-ace8-4fbc-846c-1794067fddec'
cookies_path = 'cookies'
cookies_file_name = 'tidal_cookies'
work_dir = 'C:/Users/trist/Music/musicdl_outputs'
cookies_full_path = cookies_path + '/' + cookies_file_name

if __name__ == '__main__':
    tidal_cookies = ''

    if not os.path.exists(cookies_path):
        os.makedirs(cookies_path)

    valid_cookies_exist = False

    if os.path.exists(cookies_full_path):
        with open(cookies_full_path, 'r') as file:
            tidal_cookies = json.loads(file.read())
        print('read cookies from file')
        expiry_time = datetime.fromisoformat(tidal_cookies['expires'])
        if expiry_time > datetime.now():
            valid_cookies_exist = True
        else:
            print("existing cookies expired")

    if not valid_cookies_exist:
        tidal_cookies = buildtidalcookies()
        with open(cookies_full_path, 'w') as file:
            file.write(json.dumps(tidal_cookies, indent=2))

        print('wrote cookies to file')

    music_client_cfg = {
            'TIDALMusicClient': {
                'default_parse_cookies': tidal_cookies,
                'work_dir': work_dir
            },
            'SpotifyMusicClient': {
                'work_dir': work_dir
            }
        }

    clients_threadings = {
            "TIDALMusicClient": 1
        }
    
    print("creating client...")
    music_client = musicdl.MusicClient(['TIDALMusicClient', 'SpotifyMusicClient'], music_client_cfg, clients_threadings)
    
    while True:
        playlist_url = input("enter playlist url:")
        print("parsing playlist...")
        song_infos = music_client.parseplaylist(playlist_url)

        num_songs = len(song_infos)
        
        print("found " + str(num_songs) + " in playlist")

        print("downloading...")
        music_client.download(song_infos)


