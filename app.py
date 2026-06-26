import os
import math
import subprocess
import uuid
import threading
import time
from flask import Flask, request, jsonify, send_file, render_template
import os
os.environ["PATH"] = os.path.expanduser("~/ffmpeg") + ":" + os.environ.get("PATH", "")  # ADD THIS
import math
import subprocess
...

app = Flask(__name__)

UPLOAD_FOLDER = '/tmp/uploads'
OUTPUT_FOLDER = '/tmp/outputs'
CHUNK_FOLDER  = '/tmp/chunks'

for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, CHUNK_FOLDER]:
    os.makedirs(d, exist_ok=True)

# ── helpers ────────────────────────────────────────────────────────────────────

def get_duration(filepath):
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0

def format_duration(s):
    h, m, sec = int(s//3600), int((s%3600)//60), int(s%60)
    if h:   return f"{h}h {m}m {sec}s"
    if m:   return f"{m}m {sec}s"
    return f"{sec}s"

def cleanup_old_files(folder, max_age_sec=3600):
    """Delete files older than max_age_sec (default 1 hour)."""
    now = time.time()
    for root, dirs, files in os.walk(folder):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if now - os.path.getmtime(fp) > max_age_sec:
                    os.remove(fp)
            except Exception:
                pass
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                if not os.listdir(dp):
                    os.rmdir(dp)
            except Exception:
                pass

def run_cleanup_loop():
    while True:
        time.sleep(1800)
        for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER, CHUNK_FOLDER]:
            cleanup_old_files(folder)

threading.Thread(target=run_cleanup_loop, daemon=True).start()

ALLOWED = {'mp4', 'mov', 'mkv', 'webm', 'avi', 'flv', 'm4v'}

def allowed(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED

# ── routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

# ── chunked upload ─────────────────────────────────────────────────────────────

@app.route('/upload/chunk', methods=['POST'])
def upload_chunk():
    """Receive one chunk. Frontend sends: upload_id, chunk_index, total_chunks, chunk (file)."""
    upload_id   = request.form.get('upload_id')
    chunk_index = int(request.form.get('chunk_index', 0))
    chunk_file  = request.files.get('chunk')
    if not upload_id or not chunk_file:
        return jsonify({'error': 'Missing params'}), 400

    chunk_dir = os.path.join(CHUNK_FOLDER, upload_id)
    os.makedirs(chunk_dir, exist_ok=True)
    chunk_file.save(os.path.join(chunk_dir, f"{chunk_index:06d}"))
    return jsonify({'ok': True})

@app.route('/upload/finalise', methods=['POST'])
def upload_finalise():
    """Assemble chunks → single file, return session info."""
    data        = request.json
    upload_id   = data.get('upload_id')
    total       = int(data.get('total_chunks', 0))
    filename    = data.get('filename', 'video.mp4')
    if not upload_id or not total:
        return jsonify({'error': 'Missing params'}), 400
    if not allowed(filename):
        return jsonify({'error': 'File type not allowed'}), 400

    ext        = filename.rsplit('.', 1)[1].lower()
    session_id = str(uuid.uuid4())
    out_path   = os.path.join(UPLOAD_FOLDER, f"{session_id}.{ext}")
    chunk_dir  = os.path.join(CHUNK_FOLDER, upload_id)

    with open(out_path, 'wb') as out:
        for i in range(total):
            cp = os.path.join(chunk_dir, f"{i:06d}")
            if not os.path.exists(cp):
                return jsonify({'error': f'Chunk {i} missing'}), 400
            with open(cp, 'rb') as cf:
                out.write(cf.read())

    # delete chunks
    for i in range(total):
        try: os.remove(os.path.join(chunk_dir, f"{i:06d}"))
        except: pass
    try: os.rmdir(chunk_dir)
    except: pass

    duration = get_duration(out_path)
    size_mb  = os.path.getsize(out_path) / (1024 * 1024)

    safe_name = ''.join(c for c in filename if c.isalnum() or c in '._- ')
    return jsonify({
        'session_id':    session_id,
        'original_name': safe_name,
        'ext':           ext,
        'duration':      duration,
        'duration_str':  format_duration(duration),
        'size_mb':       round(size_mb, 1),
    })

# ── processing ─────────────────────────────────────────────────────────────────

def input_path(session_id, ext):
    return os.path.join(UPLOAD_FOLDER, f"{session_id}.{ext}")

def make_out_dir():
    s = str(uuid.uuid4())
    d = os.path.join(OUTPUT_FOLDER, s)
    os.makedirs(d)
    return s, d

def ffmpeg_run(cmd):
    subprocess.run(['ffmpeg', '-y'] + cmd, capture_output=True)

@app.route('/split', methods=['POST'])
def split():
    data          = request.json
    session_id    = data.get('session_id')
    ext           = data.get('ext')
    mode          = data.get('mode')
    value         = float(data.get('value', 5))
    original_name = data.get('original_name', 'video').rsplit('.', 1)[0]
    also_mp3      = data.get('also_mp3', True)
    mp3_quality   = str(data.get('mp3_quality', '2'))
    mp3_rate      = str(data.get('mp3_rate', '44100'))

    inp = input_path(session_id, ext)
    if not os.path.exists(inp):
        return jsonify({'error': 'File not found — please re-upload'}), 404

    duration = get_duration(inp)
    size_mb  = os.path.getsize(inp) / (1024 * 1024)

    if   mode == 'duration': seg_dur = value * 60
    elif mode == 'parts':    seg_dur = duration / max(1, int(value))
    elif mode == 'size':     seg_dur = duration / max(1, math.ceil(size_mb / value))
    else: return jsonify({'error': 'Invalid mode'}), 400

    segments = math.ceil(duration / seg_dur)
    out_session, out_dir = make_out_dir()
    files = []

    for i in range(segments):
        start   = i * seg_dur
        vname   = f"{original_name}_part{i+1}.{ext}"
        vpath   = os.path.join(out_dir, vname)
        ffmpeg_run(['-ss', str(start), '-i', inp, '-t', str(seg_dur),
                    '-c', 'copy', '-avoid_negative_ts', '1', vpath])
        vsize = os.path.getsize(vpath) / (1024*1024)
        files.append({'filename': vname, 'out_session': out_session,
                      'size_mb': round(vsize,1), 'label': f"Part {i+1} of {segments}", 'type': 'video'})

        if also_mp3:
            mname = f"{original_name}_part{i+1}.mp3"
            mpath = os.path.join(out_dir, mname)
            ffmpeg_run(['-ss', str(start), '-i', inp, '-t', str(seg_dur),
                        '-vn', '-acodec', 'libmp3lame', '-q:a', mp3_quality, '-ar', mp3_rate, mpath])
            msize = os.path.getsize(mpath) / (1024*1024)
            files.append({'filename': mname, 'out_session': out_session,
                          'size_mb': round(msize,1), 'label': f"Part {i+1} MP3", 'type': 'mp3'})

    return jsonify({'files': files})

@app.route('/mp3', methods=['POST'])
def extract_mp3():
    data          = request.json
    session_id    = data.get('session_id')
    ext           = data.get('ext')
    quality       = str(data.get('quality', '2'))
    sample_rate   = str(data.get('sample_rate', '44100'))
    original_name = data.get('original_name', 'audio').rsplit('.', 1)[0]

    inp = input_path(session_id, ext)
    if not os.path.exists(inp):
        return jsonify({'error': 'File not found — please re-upload'}), 404

    out_session, out_dir = make_out_dir()
    mname = f"{original_name}.mp3"
    mpath = os.path.join(out_dir, mname)
    ffmpeg_run(['-i', inp, '-vn', '-acodec', 'libmp3lame', '-q:a', quality, '-ar', sample_rate, mpath])
    msize = os.path.getsize(mpath) / (1024*1024)
    return jsonify({'files': [{'filename': mname, 'out_session': out_session,
                                'size_mb': round(msize,1), 'label': 'MP3 audio'}]})

@app.route('/trim', methods=['POST'])
def trim():
    data          = request.json
    session_id    = data.get('session_id')
    ext           = data.get('ext')
    start         = float(data.get('start', 0))
    end           = float(data.get('end', 60))
    original_name = data.get('original_name', 'video').rsplit('.', 1)[0]

    if end <= start:
        return jsonify({'error': 'End time must be after start time'}), 400

    inp = input_path(session_id, ext)
    if not os.path.exists(inp):
        return jsonify({'error': 'File not found — please re-upload'}), 404

    out_session, out_dir = make_out_dir()
    vname = f"{original_name}_trimmed.{ext}"
    vpath = os.path.join(out_dir, vname)
    ffmpeg_run(['-ss', str(start), '-i', inp, '-t', str(end-start), '-c', 'copy', vpath])
    vsize = os.path.getsize(vpath) / (1024*1024)
    return jsonify({'files': [{'filename': vname, 'out_session': out_session,
                                'size_mb': round(vsize,1), 'label': 'Trimmed clip'}]})

@app.route('/download/<out_session>/<filename>')
def download(out_session, filename):
    # basic path traversal guard
    if '..' in out_session or '..' in filename:
        return 'Bad request', 400
    path = os.path.join(OUTPUT_FOLDER, out_session, filename)
    if not os.path.exists(path):
        return 'File not found', 404
    return send_file(path, as_attachment=True, download_name=filename)

if __name__ == '__main__':
    app.run(debug=False, port=5050)
