"""Capture our authored media through stock libVLC video callbacks, with navigation.

This is a player API integration test, not a screenshot of another app.
"""
import argparse,ctypes as C,json,os,threading,time,sys,subprocess,hashlib,re
from collections import OrderedDict
from pathlib import Path
from PIL import Image
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('vlc_root',type=Path);p.add_argument('disc',type=Path)
p.add_argument('--output',type=Path,default=Path('work/libvlc-capture'))
p.add_argument('--actions',default='down,activate',help='Comma separated navigation, menu-ready, pgc-ready=key, subtitles-ready=count, seek=ms, wait=seconds, pause, play, audio=index, subtitle=index/none, chapter=index actions')
p.add_argument('--java-home',type=Path)
p.add_argument('--seek-ms',type=int,help='Seek after navigation actions, for cell transition tests')
p.add_argument('--wait',type=float,default=3,help='Initial wait in seconds')
p.add_argument('--native-snapshot',action='store_true',help='Use the normal VLC video output and snapshot API')
p.add_argument('--vout',help='Optional normal video output module for snapshot testing')
p.add_argument('--timeout',type=float,default=300,help='Hard timeout for our child process')
p.add_argument('--ready-timeout',type=float,default=180,help='Deadline for each DVD menu/PGC readiness action, including original intros')
p.add_argument('--audio',action='store_true',help='Decode audio to memory callbacks for A/V testing')
p.add_argument('--child',action='store_true',help=argparse.SUPPRESS)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
def write_result(result):
    target=a.output/'result.json';temporary=target.with_suffix('.tmp')
    temporary.write_text(json.dumps(result,indent=2))
    os.replace(temporary,target)
root=a.vlc_root.resolve();os.environ['VLC_PLUGIN_PATH']=str(root/'plugins')
os.environ['BD_DEBUG_MASK']='0xffff';os.environ['BD_DEBUG_FILE']=str((a.output/'libbluray.log').resolve())
if a.java_home:os.environ['JAVA_HOME']=str(a.java_home.resolve())
if not a.child:
    # VLC uses a different Windows C runtime from CPython. Supply environment
    # at process creation so its getenv snapshot receives BD-J settings.
    try:
        sys.exit(subprocess.run([sys.executable,*sys.argv,'--child'],env=os.environ.copy(),timeout=a.timeout).returncode)
    except subprocess.TimeoutExpired:
        checkpoint=a.output/'result.json'
        try:result=json.loads(checkpoint.read_text())
        except (OSError,ValueError):result={}
        result.update(error='Player integration test timed out',in_progress=False)
        write_result(result)
        sys.exit(2)
dll_dir=os.add_dll_directory(str(root)) if os.name=='nt' else None
if os.name=='nt':
    core=C.CDLL(str(root/'libvlccore.dll'))
v=C.CDLL(str(root/('libvlc.dll' if os.name=='nt' else 'libvlc.so')))
def api(name,result,args):
 f=getattr(v,name);f.restype=result;f.argtypes=args;return f
ptr=C.c_void_p
new=api('libvlc_new',ptr,[C.c_int,C.POINTER(C.c_char_p)])
media=api('libvlc_media_new_location',ptr,[ptr,C.c_char_p])
player_new=api('libvlc_media_player_new_from_media',ptr,[ptr])
play=api('libvlc_media_player_play',C.c_int,[ptr])
stop=api('libvlc_media_player_stop',None,[ptr])
nav=api('libvlc_media_player_navigate',None,[ptr,C.c_uint])
state=api('libvlc_media_player_get_state',C.c_int,[ptr])
release=api('libvlc_release',None,[ptr])
release_player=api('libvlc_media_player_release',None,[ptr])
release_media=api('libvlc_media_release',None,[ptr])
width,height=960,540
latest=[None,0]
buffers={};recent=OrderedDict();picture_serial=[0]
y_size=width*544;uv_size=(width//2)*288
audio_samples=[0]
lock=threading.Lock()
LOCK=C.CFUNCTYPE(ptr,ptr,C.POINTER(ptr));UNLOCK=C.CFUNCTYPE(None,ptr,ptr,C.POINTER(ptr));DISPLAY=C.CFUNCTYPE(None,ptr,ptr)
@LOCK
def lock_frame(opaque,planes):
 # Each queued picture owns aligned planes. Sharing one buffer between vouts
 # can capture overwritten pixels instead of the displayed picture.
 storage=C.create_string_buffer(y_size+2*uv_size+31)
 base=(C.addressof(storage)+31)&~31
 with lock:
  picture_serial[0]+=1;token=picture_serial[0];buffers[token]=(storage,base)
 planes[0]=base;planes[1]=base+y_size;planes[2]=base+y_size+uv_size
 return token
@UNLOCK
def unlock_frame(opaque,picture,planes):pass
@DISPLAY
def display_frame(opaque,picture):
 with lock:
  allocated=buffers.pop(picture,None)
  if allocated:
   storage,base=allocated
   pixels=C.string_at(base,width*height)+C.string_at(base+y_size,width*height//4)+C.string_at(base+y_size+uv_size,width*height//4)
   recent[picture]=pixels
   while len(recent)>8:recent.popitem(last=False)
  else:pixels=recent.get(picture)
  if pixels is not None:latest[0]=pixels;latest[1]+=1
opts=[b'--ignore-config',b'--no-video-title-show',b'--avcodec-hw=none',b'--no-osd',
      b'--verbose=2',b'--file-logging',('--logfile='+str((a.output/'vlc.log').resolve())).encode()]
if a.vout:opts.append(('--vout='+a.vout).encode())
if not a.audio:opts.append(b'--no-audio')
instance=new(len(opts),(C.c_char_p*len(opts))(*opts))
if not instance:raise RuntimeError('libVLC instance failed')
m=media(instance,('bluray:///'+a.disc.resolve().as_posix()).encode());mp=player_new(m)
get_time=api('libvlc_media_player_get_time',C.c_int64,[ptr])
class Track(C.Structure):pass
Track._fields_=[('id',C.c_int),('name',C.c_char_p),('next',C.POINTER(Track))]
def audio_tracks():
 head=api('libvlc_audio_get_track_description',C.POINTER(Track),[ptr])(mp);node=head;result=[]
 while node:
  result.append(dict(id=node.contents.id,name=(node.contents.name or b'').decode('utf8','replace')));node=node.contents.next
 if head:api('libvlc_track_description_list_release',None,[C.POINTER(Track)])(head)
 return result
def subtitle_tracks():
 head=api('libvlc_video_get_spu_description',C.POINTER(Track),[ptr])(mp);node=head;result=[]
 while node:
  result.append(dict(id=node.contents.id,name=(node.contents.name or b'').decode('utf8','replace')));node=node.contents.next
 if head:api('libvlc_track_description_list_release',None,[C.POINTER(Track)])(head)
 return result
if a.audio:
 AUDIO=C.CFUNCTYPE(None,ptr,ptr,C.c_uint,C.c_int64)
 AUDIO_TIME=C.CFUNCTYPE(None,ptr,C.c_int64)
 AUDIO_DRAIN=C.CFUNCTYPE(None,ptr)
 @AUDIO
 def audio_play(opaque,samples,count,pts):audio_samples[0]+=count
 @AUDIO_TIME
 def audio_pause(opaque,pts):pass
 @AUDIO_TIME
 def audio_resume(opaque,pts):pass
 @AUDIO_TIME
 def audio_flush(opaque,pts):pass
 @AUDIO_DRAIN
 def audio_drain(opaque):pass
 api('libvlc_audio_set_callbacks',None,[ptr,AUDIO,AUDIO_TIME,AUDIO_TIME,AUDIO_TIME,AUDIO_DRAIN,ptr])(mp,audio_play,audio_pause,audio_resume,audio_flush,audio_drain,None)
 api('libvlc_audio_set_format',None,[ptr,C.c_char_p,C.c_uint,C.c_uint])(mp,b'S16N',48000,2)
if not a.native_snapshot:
 api('libvlc_video_set_callbacks',None,[ptr,LOCK,UNLOCK,DISPLAY,ptr])(mp,lock_frame,unlock_frame,display_frame,None)
# Planar pitches must be explicit. video_set_format applies its one pitch to
# every plane, which would overflow a compact I420 allocation.
FORMAT=C.CFUNCTYPE(C.c_uint,C.POINTER(ptr),C.POINTER(C.c_char),C.POINTER(C.c_uint),C.POINTER(C.c_uint),C.POINTER(C.c_uint),C.POINTER(C.c_uint))
CLEANUP=C.CFUNCTYPE(None,ptr)
@FORMAT
def format_frame(opaque,chroma,w,h,pitches,lines):
 C.memmove(chroma,b'I420',4);w[0]=width;h[0]=height
 pitches[0]=width;pitches[1]=pitches[2]=width//2
 lines[0]=544;lines[1]=lines[2]=288
 return 1
@CLEANUP
def cleanup_frame(opaque):pass
if not a.native_snapshot:
 api('libvlc_video_set_format_callbacks',None,[ptr,FORMAT,CLEANUP])(mp,format_frame,cleanup_frame)
def save(name):
 if a.native_snapshot:
  code=api('libvlc_video_take_snapshot',C.c_int,[ptr,C.c_uint,C.c_char_p,C.c_uint,C.c_uint])(mp,0,str((a.output/(name+'.png')).resolve()).encode(),width,height)
  if code:raise RuntimeError('Native VLC snapshot failed')
  time.sleep(.3)
  return
 with lock:b=latest[0]
 if b:
  subprocess.run(['ffmpeg','-v','error','-y','-f','rawvideo','-pixel_format','yuv420p',
                  '-video_size',f'{width}x{height}','-i','pipe:0','-frames:v','1',str(a.output/(name+'.png'))],input=b,check=True)
 else:print('No video frame for',name,flush=True)
observations=[];startup_seconds=None
# Read each completed log line once. Long original startup sequences and looping
# menus produce large logs; rescanning them every 100 ms can delay the player.
log_offset=0;log_pending=b'';last_navigation=None;last_menu_event=None
def navigation_events():
 global log_offset,log_pending,last_navigation,last_menu_event
 logfile=a.output/'libbluray.log'
 if not logfile.exists():return last_navigation,last_menu_event
 with logfile.open('rb') as stream:
  if stream.seek(0,2)<log_offset:
   log_offset=0;log_pending=b'';last_navigation=None;last_menu_event=None
  stream.seek(log_offset);chunk=stream.read();log_offset=stream.tell()
 lines=(log_pending+chunk).split(b'\n');log_pending=lines.pop()
 for line in lines:
  match=re.search(rb'DVD2UHD (PLAY|CONTINUE|SYNC|SEEK) ([^ ]+) cell=',line)
  if match:last_navigation=match[2].decode('utf8','replace')
  match=re.search(rb'DVD2UHD (PLAY [^\r\n]+|MENU buttons=[^\r\n]+)',line)
  if match:last_menu_event=match[1].decode('utf8','replace')
 return last_navigation,last_menu_event
try:
 if play(mp):raise RuntimeError('libVLC play failed')
 started=time.monotonic();deadline=started+min(45,a.timeout/2)
 if not a.native_snapshot:
  while latest[1]==0 and time.monotonic()<deadline:
   if state(mp)==7:raise RuntimeError('libVLC entered error state during startup')
   time.sleep(.1)
  if latest[1]==0:raise RuntimeError('No decoded frame before startup deadline')
 startup_seconds=time.monotonic()-started
 def observe(action):
  with lock:frame_hash=hashlib.sha256(latest[0]).hexdigest() if latest[0] else None
  observations.append(dict(action=action,time_ms=get_time(mp),frames=latest[1],audio_samples=audio_samples[0],state=state(mp),
                           length_ms=api('libvlc_media_player_get_length',C.c_int64,[ptr])(mp),
                           position=api('libvlc_media_player_get_position',C.c_float,[ptr])(mp),
                           audio_track=api('libvlc_audio_get_track',C.c_int,[ptr])(mp),audio_tracks=audio_tracks(),
                           subtitle_track=api('libvlc_video_get_spu',C.c_int,[ptr])(mp),subtitle_tracks=subtitle_tracks(),
                           chapter=api('libvlc_media_player_get_chapter',C.c_int,[ptr])(mp),
                           chapter_count=api('libvlc_media_player_get_chapter_count',C.c_int,[ptr])(mp),frame_sha256=frame_hash))
  # Journal each completed action before a later wait can hit the hard limit.
  write_result(dict(in_progress=True,frames=latest[1],audio_samples=audio_samples[0],
      startup_seconds=startup_seconds,state=state(mp),observations=observations,output=str(a.output.resolve())))
 time.sleep(a.wait);save('initial');observe('initial')
 for i,action in enumerate(filter(None,a.actions.split(','))):
  wait=2
  if action=='menu-ready' or action.startswith('pgc-ready='):
   deadline=time.monotonic()+a.ready_timeout
   while True:
    current,menu_event=navigation_events()
    ready=(bool(menu_event) and menu_event.startswith('MENU buttons=')) if action=='menu-ready' else current==action[10:]
    if state(mp)==7:raise RuntimeError('libVLC error while waiting for '+action)
    if ready:break
    if time.monotonic()>=deadline:raise RuntimeError('DVD readiness deadline: '+action)
    time.sleep(.1)
   wait=.3
  elif action.startswith('subtitles-ready='):
   needed=int(action.split('=',1)[1]);deadline=time.monotonic()+a.ready_timeout
   while len([t for t in subtitle_tracks() if t['id']>=0])<needed:
    if time.monotonic()>=deadline:raise RuntimeError('Native subtitle inventory deadline: '+action)
    if state(mp)==7:raise RuntimeError('libVLC error while waiting for subtitle inventory')
    time.sleep(.1)
   wait=.3
  elif action.startswith('seek='):api('libvlc_media_player_set_time',None,[ptr,C.c_int64])(mp,int(action[5:]))
  elif action.startswith('wait='):wait=float(action[5:])
  elif action=='pause':api('libvlc_media_player_set_pause',None,[ptr,C.c_int])(mp,1)
  elif action=='play':api('libvlc_media_player_set_pause',None,[ptr,C.c_int])(mp,0)
  elif action.startswith('audio='):
   tracks=[t for t in audio_tracks() if t['id']>=0];ordinal=int(action[6:])
   if not 0<=ordinal<len(tracks):raise ValueError('Audio ordinal unavailable')
   api('libvlc_audio_set_track',C.c_int,[ptr,C.c_int])(mp,tracks[ordinal]['id'])
  elif action.startswith('chapter='):api('libvlc_media_player_set_chapter',None,[ptr,C.c_int])(mp,int(action[8:]))
  elif action.startswith('subtitle='):
   choice=action[9:];tracks=[t for t in subtitle_tracks() if t['id']>=0]
   if choice=='none':track=-1
   else:
    ordinal=int(choice)
    if not 0<=ordinal<len(tracks):raise ValueError('Subtitle ordinal unavailable')
    track=tracks[ordinal]['id']
   if api('libvlc_video_set_spu',C.c_int,[ptr,C.c_int])(mp,track)!=0:raise RuntimeError('Native subtitle selection failed')
  else:nav(mp,{'activate':0,'up':1,'down':2,'left':3,'right':4,'menu':5}[action])
  time.sleep(wait);save(str(i+1)+'-'+re.sub(r'[^a-zA-Z0-9_.-]','-',action));observe(action)
 if a.seek_ms is not None:
  api('libvlc_media_player_set_time',None,[ptr,C.c_int64])(mp,a.seek_ms)
  time.sleep(5);save('seek')
 result=dict(frames=latest[1],audio_samples=audio_samples[0],startup_seconds=startup_seconds,state=state(mp),observations=observations,callback_model='per-picture-aligned-i420',output=str(a.output.resolve()))
 write_result(result)
 print(json.dumps(result),flush=True)
except Exception as error:
 result=dict(error=str(error),frames=latest[1],audio_samples=audio_samples[0],startup_seconds=startup_seconds,state=state(mp),observations=observations,output=str(a.output.resolve()))
 write_result(result)
 raise
finally:
 stop(mp);release_player(mp);release_media(m);release(instance)
