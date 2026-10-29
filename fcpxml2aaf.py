#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FCPXML -> AAF (embedded audio) converter.
Runs a tiny local web UI on 127.0.0.1 — nothing leaves the machine.
Requires: python3 (3.8+). Bundled: pyaaf2 (./lib). Optional: afconvert (macOS) or ffmpeg for mp3/aiff/float wav.
"""
import os, sys, json, threading, tempfile, shutil, subprocess, struct, time, re, webbrowser, traceback, unicodedata
from fractions import Fraction
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'lib'))
import aaf2  # noqa: E402
from updater import Updater  # noqa: E402

PORT = 4848
APP_ID = 'tzv'
LOGF = os.path.join(HERE, 'app.log')


def applog(m):
    try:
        with open(LOGF, 'a', encoding='utf8') as f:
            f.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), m))
    except Exception:
        pass


UPD = Updater(HERE, applog)

# ------------------------------------------------------------------ FCPXML parse
def ptime(s):
    if s is None or s == '':
        return None
    s = str(s).strip()
    if s.endswith('s'):
        s = s[:-1]
    if '/' in s:
        a, b = s.split('/')
        return Fraction(int(a), int(b))
    return Fraction(s)


def pdb(s):
    if s is None:
        return 0.0
    try:
        return float(str(s).lower().replace('db', ''))
    except ValueError:
        return 0.0


def decode_src(src):
    if not src:
        return ''
    p = re.sub(r'^file://(localhost)?', '', src, flags=re.I)
    return unicodedata.normalize('NFC', unquote(p))


CLIPLIKE = {'asset-clip', 'audio', 'clip', 'ref-clip', 'sync-clip', 'mc-clip', 'video', 'gap', 'title'}
INF = Fraction(10 ** 12)


def parse_fcpxml(text):
    root = ET.fromstring(text)
    if root.tag != 'fcpxml':
        raise ValueError('Це не FCPXML-файл')
    warnings = []
    assets, formats, medias = {}, {}, {}
    res = root.find('resources')
    for r in (list(res) if res is not None else []):
        if r.tag == 'format':
            formats[r.get('id')] = ptime(r.get('frameDuration'))
        elif r.tag == 'asset':
            reps = r.findall('media-rep')
            rep = next((e for e in reps if (e.get('kind') or 'original-media') == 'original-media'), reps[0] if reps else None)
            assets[r.get('id')] = dict(
                id=r.get('id'), name=r.get('name') or '',
                path=decode_src(rep.get('src') if rep is not None else r.get('src')),
                duration=float(ptime(r.get('duration')) or 0),
                channels=int(r.get('audioChannels') or 2), sources=int(r.get('audioSources') or 1),
                hasAudio=r.get('hasAudio') == '1', hasVideo=r.get('hasVideo') == '1')
        elif r.tag == 'media':
            medias[r.get('id')] = r

    proj = root.find('.//project')
    seq = proj.find('sequence') if proj is not None else root.find('.//sequence')
    if seq is None:
        raise ValueError('У файлі немає sequence')
    name = proj.get('name') if proj is not None else 'Project'
    fd = formats.get(seq.get('format'))
    fps = float(1 / fd) if fd else 25.0
    seq_dur = ptime(seq.get('duration')) or Fraction(0)
    tc_start = ptime(seq.get('tcStart')) or Fraction(0)

    clips, vspans, stats = [], [], {'auto': 0, 'mc': 0}

    def vol(el):
        db, fi, fo, kf = 0.0, Fraction(0), Fraction(0), []
        for c in el.findall('adjust-volume'):
            db += pdb(c.get('amount'))
            f1, f2 = c.find('.//fadeIn'), c.find('.//fadeOut')
            if f1 is not None:
                fi = ptime(f1.get('duration')) or 0
            if f2 is not None:
                fo = ptime(f2.get('duration')) or 0
            for k in c.iter('keyframe'):
                if k.get('time') is not None and k.get('value') is not None:
                    kf.append((ptime(k.get('time')), pdb(k.get('value'))))
            if kf:
                stats['auto'] += 1
        return db, fi, fo, sorted(kf)

    def chan_parts(el, asset):
        acs = el.findall('audio-channel-source') if el is not None else []
        parts, roles = [], [el.get('audioRole') or ''] if el is not None else ['']
        for a_ in acs:
            if a_.get('active') == '0' or a_.get('enabled') == '0':
                continue
            chs = [int(x) for x in re.findall(r'\d+', a_.get('srcCh') or '')]
            if chs:
                parts.append(chs); roles.append(a_.get('role') or '')
        if acs and not parts:
            return [], roles
        if not parts:
            ch, src = max(1, asset['channels']), max(1, asset['sources'])
            if src > 1:
                parts = [list(range(i * ch + 1, (i + 1) * ch + 1)) for i in range(src)]
            else:
                parts = [list(range(1, ch + 1))]
        return parts, roles

    def add_audio(asset, abs_, inpt, dur, ctx, nm, el=None):
        if not asset:
            return
        lo, hi = max(abs_, ctx['lo']), min(abs_ + dur, ctx['hi'])
        if hi - lo <= 0:
            return
        parts, roles = chan_parts(el, asset)
        if not parts:
            return
        clips.append(dict(name=nm or asset['name'], asset=asset, time=lo, duration=hi - lo,
                          playStart=inpt + (lo - abs_), db=ctx['db'], enabled=ctx['enabled'],
                          lane=ctx['lane'], fadeIn=ctx['fi'], fadeOut=ctx['fo'], parts=parts, kf=ctx.get('kf') or [],
                          role=' '.join(r for r in roles if r)))

    def child_abs(ch, ctx):
        return ctx['abs'] + ((ptime(ch.get('offset')) or 0) - ctx['pstart'])

    def visit(el, ctx):
        n = el.tag
        if n == 'spine':
            so = ptime(el.get('offset'))
            lane = ((ctx['lane'] + '.') if ctx['lane'] not in ('0', '') else '') + el.get('lane') if el.get('lane') is not None else ctx['lane']
            first = next((c for c in el if c.get('offset') is not None), None)
            foff = ptime(first.get('offset')) if first is not None else 0
            if so is not None and foff < so:
                nctx = dict(ctx, abs=child_abs(el, ctx), pstart=Fraction(0), lane=lane)
            else:
                nctx = dict(ctx, lane=lane)
            for c in el:
                visit(c, nctx)
            return
        if n not in CLIPLIKE:
            return
        if n == 'mc-clip':
            stats['mc'] += 1
            return
        abs_ = child_abs(el, ctx)
        start = ptime(el.get('start')) or Fraction(0)
        dur = ptime(el.get('duration')) or Fraction(0)
        conn = el.get('lane') is not None
        lane = ((ctx['lane'] + '.') if ctx['lane'] not in ('0', '') else '') + el.get('lane') if conn else (ctx['lane'] or '0')
        enabled = ctx['enabled'] and el.get('enabled') != '0'
        db, fi, fo, kf = vol(el)
        lo = -INF if conn else ctx['lo']
        hi = INF if conn else ctx['hi']
        win_lo, win_hi = max(lo, abs_), min(hi, abs_ + dur)
        my = dict(abs=abs_, pstart=start, lane=lane, enabled=enabled, db=ctx['db'] + db, fi=fi, fo=fo, kf=kf, lo=win_lo, hi=win_hi)
        if n == 'asset-clip':
            a = assets.get(el.get('ref'))
            if a and a['hasVideo'] and enabled:
                vspans.append((win_lo, win_hi))
            if a and (a['hasAudio'] or not a['hasVideo']) and el.get('audioRole') != 'none':
                ast, adu = ptime(el.get('audioStart')), ptime(el.get('audioDuration'))
                if ast is not None and adu is not None:
                    add_audio(a, abs_ + (ast - start), ast, adu, dict(my, lo=lo, hi=hi), el.get('name'), el)
                else:
                    add_audio(a, abs_, start, dur, my, el.get('name'), el)
        elif n == 'audio':
            add_audio(assets.get(el.get('ref')), abs_, start, dur, my, el.get('name'), el)
        elif n == 'video':
            if enabled:
                vspans.append((win_lo, win_hi))
        elif n == 'ref-clip':
            m = medias.get(el.get('ref'))
            sp = m.find('sequence/spine') if m is not None else None
            if sp is not None:
                for c in sp:
                    visit(c, my)
        for c in el:
            if c.tag == 'spine' or c.tag in CLIPLIKE:
                visit(c, my)

    spine = seq.find('spine')
    base = dict(abs=Fraction(0), pstart=Fraction(0), lane='0', enabled=True, db=0.0, fi=0, fo=0, kf=[], lo=-INF, hi=INF)
    for c in (list(spine) if spine is not None else []):
        visit(c, base)

    for i, c in enumerate(clips):
        c['id'] = i
    if stats['auto']:
        warnings.append('Ключі гучності є у %d кліп(ах) — переносяться з опцією «Гучність як автоматизація».' % stats['auto'])
    if stats['mc']:
        warnings.append('Пропущено мультикам-кліпів: %d.' % stats['mc'])
    return dict(name=name, fps=fps, seqDur=seq_dur, tcStart=tc_start, clips=clips, vspans=vspans, warnings=warnings)


def filter_clips(p, drop_disabled=False, drop_outside=False):
    out = [c for c in p['clips'] if c['enabled'] or not drop_disabled]
    if drop_outside:
        lo, hi = Fraction(0), p['seqDur']
        if p['vspans']:
            lo, hi = min(s[0] for s in p['vspans']), max(s[1] for s in p['vspans'])
        res = []
        for c in out:
            a, b = max(c['time'], lo), min(c['time'] + c['duration'], hi)
            if b - a <= 0:
                continue
            res.append(dict(c, time=a, duration=b - a, playStart=c['playStart'] + (a - c['time']),
                            fadeIn=0 if a > c['time'] else c['fadeIn'],
                            fadeOut=0 if b < c['time'] + c['duration'] else c['fadeOut']))
        out = res
    return out


def lane_key(l):
    return [float(x) for x in l.split('.')]


def fmt_tc(sec, fps):
    fr = int(round(fps))
    f = int(round(float(sec) * fps))
    return '%02d:%02d:%02d:%02d' % (f // (3600 * fr), (f // (60 * fr)) % 60, (f // fr) % 60, f % fr)


SFX_WORDS = ('swish', 'swoosh', 'whoosh', 'woosh', 'gleam', 'hit', 'boom', 'riser', 'impact', 'swipe', 'clang',
             'reverse', 'sweep', 'braam', 'stinger', 'sfx', 'fx', 'transition', 'click', 'pop', 'glitch')
TYPE_ORDER = {'music': 0, 'sfx': 1}
TYPE_NAME = {'music': 'Music', 'sfx': 'SFX'}
# бібліотечна музика: каталожний префікс на початку назви — великі літери + цифри + «_»
# (Extreme: XCD737_, XEH013_, XDS…; Universal: UPM_…, BER1230A_; ATN074_ тощо) або UPM_ будь-де
LIB_MUSIC_RE = re.compile(r'^[A-Z]{2,8}\d{2,6}[A-Z]?_|(?:^|[_\- ])UPM_')


def classify(c):
    """два типи: music / sfx. Бібліотечні треки — завжди музика, хоч би які короткі; далі — довжина файлу"""
    a = c['asset']
    n = a['name'] or ''
    if LIB_MUSIC_RE.search(n):
        return 'music'
    if a.get('hasVideo'):
        return 'sfx'
    d = a['duration']
    if d >= 30:
        return 'music'
    if any(w in n.lower() for w in SFX_WORDS):
        return 'sfx'
    return 'sfx'


def build_tracks(clips, layout='min', types=None):
    """layout: fcp — лейн = доріжка; min — мінімум доріжок; tidy — Голос/Музика/Шуми, кожна група на мінімум.
    Кліпи на одній доріжці ніколи не перекриваються (тетріс: перша доріжка, де є місце)."""
    types = types or {}
    units = []
    for c in clips:
        t = types.get(str(c['id'])) or classify(c)
        for part in c['parts']:
            units.append({'c': c, 'part': part, 'w': len(part), 'type': t})

    def gkey(u):
        off = not u['c']['enabled']
        if layout == 'fcp':
            return (tuple(lane_key(u['c']['lane'])), off, -u['w'])
        if layout == 'tidy':
            return (TYPE_ORDER.get(u['type'], 9), off, -u['w'])
        return (off, -u['w'])

    groups = {}
    for u in units:
        groups.setdefault(gkey(u), []).append(u)
    tracks = []
    for k in sorted(groups):
        subs = []
        for u in sorted(groups[k], key=lambda u: (u['c']['time'], -u['c']['duration'])):
            st = u['c']['time']
            for sb in subs:
                if sb['end'] <= st:
                    sb['units'].append(u); sb['end'] = st + u['c']['duration']; break
            else:
                subs.append({'units': [u], 'end': st + u['c']['duration']})
        u0 = groups[k][0]
        off, w = not u0['c']['enabled'], u0['w']
        if layout == 'fcp':
            lane = u0['c']['lane']
            base = 'Main' if lane == '0' else 'Lane ' + lane
        elif layout == 'tidy':
            base = TYPE_NAME.get(u0['type'], 'Audio')
        else:
            base = 'Audio'
        for i, sb in enumerate(subs):
            num = (' %d' % (i + 1)) if (len(subs) > 1 or layout != 'fcp') else ''
            wtag = ' mono' if w == 1 else ('' if w == 2 else ' %dch' % w)
            tracks.append({'name': base + num + wtag + (' OFF' if off else ''), 'units': sb['units'],
                           'off': off, 'w': w, 'type': u0['type']})
    return tracks

# ------------------------------------------------------------------ audio prep
def find_tool(names):
    extra = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin']
    for n in names:
        p = shutil.which(n)
        if p:
            return p
        for d in extra:
            q = os.path.join(d, n)
            if os.path.isfile(q) and os.access(q, os.X_OK):
                return q
    return None


def real_path(p):
    for v in (p, unicodedata.normalize('NFD', p), unicodedata.normalize('NFC', p)):
        if os.path.isfile(v):
            return v
    return None


NOWIN = {'creationflags': 0x08000000} if os.name == 'nt' else {}
AFCONVERT = find_tool(['afconvert'])
FFMPEG = find_tool(['ffmpeg'])


def wav_info(path):
    """return (fmt_ok, rate, channels, bits) for plain PCM int wav, else None"""
    try:
        with open(path, 'rb') as f:
            h = f.read(12)
            if len(h) < 12 or h[:4] not in (b'RIFF', b'RF64') or h[8:12] != b'WAVE':
                return None
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    return None
                cid, sz = ch[:4], struct.unpack('<I', ch[4:])[0]
                if cid == b'fmt ':
                    d = f.read(sz)
                    tag, chans, rate = struct.unpack('<HHI', d[:8])
                    bits = struct.unpack('<H', d[14:16])[0]
                    if tag == 0xFFFE and len(d) >= 26:
                        tag = struct.unpack('<H', d[24:26])[0]
                    return (tag == 1 and bits in (16, 24, 32) and h[:4] == b'RIFF', rate, chans, bits)
                f.seek(sz + (sz & 1), 1)
    except OSError:
        return None


def convert_to_wav(src, dst, rate):
    if AFCONVERT:
        cmd = [AFCONVERT, '-f', 'WAVE', '-d', 'LEI24@%d' % rate, src, dst]
    elif FFMPEG:
        cmd = [FFMPEG, '-v', 'error', '-y', '-i', src, '-ar', str(rate), '-c:a', 'pcm_s24le', '-rf64', 'never', dst]
    else:
        raise RuntimeError('Немає afconvert/ffmpeg для конвертації ' + os.path.basename(src))
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NOWIN)
    if r.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError('Конвертація не вдалася: %s\n%s' % (os.path.basename(src), r.stderr.decode('utf8', 'ignore')[-400:]))


def read_wav_pcm(path):
    """minimal reader -> (rate, chans, sampwidth, data bytes)"""
    with open(path, 'rb') as f:
        f.read(12)
        rate = chans = bits = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                break
            cid, sz = ch[:4], struct.unpack('<I', ch[4:])[0]
            if cid == b'fmt ':
                d = f.read(sz)
                _, chans, rate = struct.unpack('<HHI', d[:8]); bits = struct.unpack('<H', d[14:16])[0]
                if sz & 1: f.read(1)
            elif cid == b'data':
                data = f.read(sz)
                return rate, chans, bits // 8, data
            else:
                f.seek(sz + (sz & 1), 1)
    raise RuntimeError('Не знайдено аудіодані у ' + path)


def write_wav(path, rate, chans, sw, data):
    ba = chans * sw
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVE')
        f.write(b'fmt ' + struct.pack('<IHHIIHH', 16, 1, chans, rate, rate * ba, ba, sw * 8))
        f.write(b'data' + struct.pack('<I', len(data)))
        f.write(data)


def split_parts(path, outdir, base, ranges, subsets):
    """ranges = [(s,e)] сек (None = цілий файл); subsets = {(1,2), (1,), (2,)…} — потрібні набори каналів.
    returns [(start_sample, n_samples, {subset: wav})]"""
    rate, chans, sw, data = read_wav_pcm(path)
    ba = chans * sw
    n = len(data) // ba
    out = []
    for ri, (s0, e0) in enumerate(ranges or [(0, None)]):
        a = max(0, int(s0 * rate)); b = n if e0 is None else min(n, int(e0 * rate) + 1)
        if b <= a:
            continue
        chunk = data[a * ba:b * ba]
        m = b - a
        files = {}
        for sub in subsets:
            sub2 = [c for c in sub if 1 <= c <= chans] or [1]
            if sub2 == list(range(1, chans + 1)):
                pcm = chunk
            else:
                k_ = len(sub2); oba = k_ * sw
                buf = bytearray(m * oba)
                for j, c in enumerate(sub2):
                    for k in range(sw):
                        buf[j * sw + k::oba] = chunk[(c - 1) * sw + k::ba]
                pcm = bytes(buf)
            op = os.path.join(outdir, '%s_r%d_%s.wav' % (base, ri, '-'.join(map(str, sub))))
            write_wav(op, rate, len(sub2), sw, pcm)
            files[sub] = op
        out.append((a, m, files))
    del data
    return out


def merge_ranges(rs, handle):
    rs = sorted((max(0.0, a - handle), b + handle) for a, b in rs)
    out = []
    for a, b in rs:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]

# ------------------------------------------------------------------ AAF writer
GAIN_OP = '9d2ea894-0968-11d3-8a38-0050040ef7d2'
AMP_PARAM = 'e4962321-2267-11d3-8a4c-0050040ef7d2'


def to_url(path):
    from urllib.parse import quote
    p = path.replace('\\', '/')
    if re.match(r'^[A-Za-z]:/', p):
        p = '/' + p
    return 'file://' + quote(p, safe='/:')


OP_COMBINER = ('6b46dd7a-132d-4856-ab21-8b751d8462ec', 'Audio Channel Combiner')
OP_GAIN = ('9d2ea894-0968-11d3-8a38-0050040ef7d2', 'Audio Gain')
OP_DISSOLVE = ('0c3bea41-fc05-11d2-8a29-0050040ef7d2', 'Avid Audio Dissolve')
PARAM_AMP = ('e4962321-2267-11d3-8a4c-0050040ef7d2', 'Amplitude')
INTERP_LINEAR = ('5b6c85a4-0ede-11d3-80a9-006008143e6f', 'LinearInterp')


def register_defs(f):
    """ті самі визначення, що пише Nuendo/Resolve"""
    defs = {}
    for key, (auid, name), n in (('comb', OP_COMBINER, 1), ('gain', OP_GAIN, 1), ('diss', OP_DISSOLVE, 2)):
        od = f.create.OperationDef(auid, name, '')
        f.dictionary.register_def(od)
        od.media_kind = 'sound'
        od['IsTimeWarp'].value = False
        od['NumberInputs'].value = n
        od['OperationCategory'].value = 'OperationCategory_Effect'
        defs[key] = od
    amp = f.create.ParameterDef(PARAM_AMP[0], PARAM_AMP[1], '', 'Rational')
    f.dictionary.register_def(amp)
    defs['gain'].parameters.append(amp)
    defs['amp'] = amp
    lin = f.create.InterpolationDef(INTERP_LINEAR[0], INTERP_LINEAR[1], '')
    f.dictionary.register_def(lin)
    defs['lin'] = lin
    return defs


def wav_summary(real, channels, sr, bits=24):
    if real:
        try:
            from aaf2.ama import get_wave_fmt
            s_ = get_wave_fmt(real)
            if s_:
                return s_
        except Exception:
            pass
    return (b'RIFF' + struct.pack('<I', 36) + b'WAVE' + b'fmt ' +
            struct.pack('<IHHIIHH', 16, 1, channels, sr, sr * channels * bits // 8, channels * bits // 8, bits))


def file_mob(f, name, path, length, sr, real=None):
    """SourceMob із посиланням на моно-WAV (як у Nuendo: один файл = один канал)"""
    sm = f.create.SourceMob()
    sm.name = name
    sl = sm.create_timeline_slot(sr)
    sc = f.create.SourceClip(media_kind='sound')
    sc.length = length
    sl.segment = sc
    d = f.create.WAVEDescriptor()
    d['SampleRate'].value = sr
    d['Summary'].value = wav_summary(real, 1, sr)
    d['Length'].value = length
    d['ContainerFormat'].value = f.dictionary.lookup_containerdef('AAF')
    loc = f.create.NetworkLocator()
    loc['URLString'].value = to_url(path)
    d['Locator'].append(loc)
    sm.descriptor = d
    f.content.mobs.append(sm)
    return sm


def master_from_files(f, name, files, length, sr, real=True):
    """files: [(channel, wav_path)] → MasterMob, {channel: slot_id}"""
    mm = f.create.MasterMob(name)
    f.content.mobs.append(mm)
    ids = {}
    for k, (ch, path) in enumerate(files):
        sm = file_mob(f, '%s_A%02d' % (name, ch), path, length, sr, path if real else None)
        ms = mm.create_timeline_slot(sr)
        ms.segment = sm.create_source_clip(1, length=length, media_kind='sound')
        ms['PhysicalTrackNumber'].value = k + 1
        ids[ch] = ms.slot_id
    return mm, ids


def write_aaf(p, clips, out_path, opts, resolve, log, progress):
    sr = int(opts.get('sampleRate') or 48000)
    media_mode = opts.get('media', 'folder')
    gain_mode = opts.get('gain', 'none')
    tracks = build_tracks(clips, opts.get('layout', 'min'), opts.get('types'))
    need, chans = {}, {}
    for c in clips:
        path = resolve(c['asset']['path'])
        need.setdefault(path, c['asset'])
        for part in c['parts']:
            chans.setdefault(path, set()).update(part)
    tmp = tempfile.mkdtemp(prefix='tzv_')
    media_dir = os.path.splitext(out_path)[0] + '_Media'
    if media_mode == 'folder':
        os.makedirs(media_dir, exist_ok=True)
    try:
        prepared, linked = {}, {}
        for i, (path, a) in enumerate(need.items()):
            progress('Готую аудіо %d/%d: %s' % (i + 1, len(need), os.path.basename(path)), i / max(1, len(need)) * 0.7)
            rp = real_path(path)
            if not rp:
                linked[path] = (a, 'немає файлу')
                continue
            info = wav_info(rp)
            src = rp
            try:
                if not info or not info[0] or info[1] != sr:
                    src = os.path.join(tmp, 'conv_%d.wav' % i)
                    convert_to_wav(rp, src, sr)
                ranges = None
                if opts.get('consolidate', True):
                    ranges = merge_ranges([(float(c['playStart']), float(c['playStart'] + c['duration']))
                                           for c in clips if resolve(c['asset']['path']) == path], float(opts.get('handles', 2)))
                subs = {(ch,) for ch in chans[path]}
                outdir = media_dir if media_mode == 'folder' else tmp
                base = re.sub(r'[\\/:*?"<>|]', '_', os.path.splitext(os.path.basename(path))[0])
                chunks = split_parts(src, tmp, 'a%d' % i, ranges, subs)
                res = []
                for k, (a0, m0, files) in enumerate(chunks):
                    nm = base + ('' if len(chunks) == 1 else '_%02d' % (k + 1))
                    fl = []
                    for (ch,), wav in sorted(files.items()):
                        dst = os.path.join(outdir, '%s_A%02d.wav' % (nm, ch))
                        shutil.move(wav, dst)
                        fl.append((ch, dst))
                    res.append((a0, m0, nm, fl))
                prepared[path] = res
            except Exception as e:
                linked[path] = (a, str(e).split('\n')[0])

        if linked:
            log('Без медіа (офлайн-кліпи, перелінкуєш у Nuendo) — %d файл(ів):' % len(linked))
            for path, (a, why) in linked.items():
                log('  • %s — %s' % (os.path.basename(path), why))

        progress('Пишу AAF…', 0.75)
        with aaf2.open(out_path, 'w') as f:
            D = register_defs(f)
            masters = {}  # path → [(start, n, mm, {ch: slot})]
            for j, (path, res) in enumerate(prepared.items()):
                masters[path] = []
                for a0, m0, nm, fl in res:
                    if media_mode == 'folder':
                        mm, ids = master_from_files(f, nm, fl, m0, sr)
                    else:
                        mm = f.create.MasterMob(nm)
                        f.content.mobs.append(mm)
                        ids = {ch: mm.import_audio_essence(w, sr).slot_id for ch, w in fl}
                    masters[path].append((a0, m0, mm, ids))
            for path, (a, why) in linked.items():
                n = int(round(a['duration'] * sr))
                base = os.path.splitext(os.path.basename(path))[0]
                wav = os.path.splitext(path)[0] + '.wav'   # Nuendo не бере посилання на mp3
                nch = max(1, a['channels'] * max(1, a['sources']))
                mm, ids = master_from_files(f, base, [(ch, wav) for ch in range(1, nch + 1)], n, sr, real=False)
                masters[path] = [(0, n, mm, ids)]

            comp = f.create.CompositionMob(p['name'])
            comp.usage = 'Usage_TopLevel'
            f.content.mobs.append(comp)
            fpsi = int(round(p['fps']))
            tcs = comp.create_timeline_slot(fpsi)
            tcs.segment = f.create.Timecode(fpsi, drop=False)
            tcs.segment.start = int(round(float(p['tcStart']) * fpsi))
            tot = max([c['time'] + c['duration'] for c in clips] + [p['seqDur']])
            tcs.segment.length = int(round(float(tot) * fpsi))

            def transition(n, cut):
                t = f.create.Transition('sound')
                t.length = n
                t['CutPoint'].value = cut
                og = f.create.OperationGroup(D['diss'], n, media_kind='sound')
                t['OperationGroup'].value = og
                return t

            placed = 0
            for t in tracks:
                slot = comp.create_sound_slot(edit_rate=sr)
                slot['SlotName'].value = t['name']
                seq = f.create.Sequence(media_kind='sound')
                slot.segment = seq
                pos = 0
                for u in t['units']:
                    c = u['c']
                    path = resolve(c['asset']['path'])
                    if path not in masters:
                        continue
                    st = int(round(float(c['time']) * sr))
                    ln = int(round(float(c['duration']) * sr))
                    ps = int(round(float(c['playStart']) * sr))
                    seg_ = next((x for x in masters[path] if x[0] <= ps < x[0] + x[1]), None)
                    if seg_ is None:
                        continue
                    a0, m0, mm, ids = seg_
                    ps -= a0
                    if st < pos:
                        cut = pos - st; st = pos; ps += cut; ln -= cut
                    ln = min(ln, m0 - ps)
                    if ln <= 0:
                        continue
                    sids = [ids[ch] for ch in u['part'] if ch in ids]
                    if not sids:
                        continue
                    if len(sids) == 1:
                        seg = mm.create_source_clip(sids[0], start=ps, length=ln)
                    else:
                        seg = f.create.OperationGroup(D['comb'], ln, media_kind='sound')
                        for sid in sids:
                            seg.segments.append(mm.create_source_clip(sid, start=ps, length=ln))
                    if gain_mode == 'auto' and (abs(c['db']) > 1e-6 or c.get('kf')):
                        g = f.create.OperationGroup(D['gain'], ln, media_kind='sound')
                        g.segments.append(seg)
                        vv = f.create.VaryingValue(D['amp'], D['lin'])
                        pts = []
                        dur = float(c['duration']) or 1.0
                        for kt, kdb in (c.get('kf') or []):
                            pts.append((min(1.0, max(0.0, (float(kt) - float(c['playStart'])) / dur)), kdb))
                        if not pts:
                            pts = [(0.0, c['db']), (1.0, c['db'])]
                        seen = set()
                        for tn, kdb in pts:
                            if tn in seen:
                                continue
                            seen.add(tn)
                            vv.add_keyframe(Fraction(tn).limit_denominator(10 ** 9), 10 ** (kdb / 20.0))
                        g.parameters.append(vv)
                        seg = g
                    fi = min(int(round(float(c['fadeIn']) * sr)), ln // 2)
                    fo = min(int(round(float(c['fadeOut']) * sr)), ln // 2)
                    gap = st - pos
                    if fi > 0:
                        seq.components.append(f.create.Filler('sound', gap + fi))
                        seq.components.append(transition(fi, 0))
                    elif gap > 0:
                        seq.components.append(f.create.Filler('sound', gap))
                    seq.components.append(seg)
                    pos = st + ln
                    if fo > 0:
                        seq.components.append(transition(fo, fo))
                        seq.components.append(f.create.Filler('sound', fo))
                    placed += 1
                seq.length = pos
        progress('Готово', 1.0)
        return dict(tracks=len(tracks), clips=placed, embedded=len(prepared), linked=len(linked),
                    missing=sorted(os.path.basename(x) for x in linked),
                    media=media_dir if media_mode == 'folder' and prepared else None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if media_mode == 'folder' and os.path.isdir(media_dir) and not os.listdir(media_dir):
            os.rmdir(media_dir)

# ------------------------------------------------------------------ web UI
STATE = {'job': None, 'ping': None}


def default_outdir():
    home = os.path.expanduser('~')
    for d in (os.path.join(os.environ.get('OneDrive', ''), 'Desktop') if os.environ.get('OneDrive') else '',
              os.path.join(home, 'Desktop'), home):
        if d and os.path.isdir(d):
            return d
    return home


def _pick_tk(initial, title='Куди зберегти AAF'):
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    root.update()
    try:
        root.lift(); root.focus_force()
    except Exception:
        pass
    d = filedialog.askdirectory(parent=root, initialdir=initial if initial and os.path.isdir(initial) else None,
                                title=title, mustexist=True)
    root.destroy()
    return os.path.normpath(d) if d else None


def _pick_ps(initial, title='Куди зберегти AAF'):
    ps = ('Add-Type -AssemblyName System.Windows.Forms;'
          '$f=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false;Opacity=0;'
          'StartPosition="CenterScreen";Width=1;Height=1};$f.Show();$f.Activate();'
          '$d=New-Object System.Windows.Forms.FolderBrowserDialog;'
          '$d.Description=$env:PICK_TITLE;$d.SelectedPath=$env:PICK_INIT;'
          '$r=$d.ShowDialog($f);$f.Close();'
          'if($r -eq [System.Windows.Forms.DialogResult]::OK){[Console]::OutputEncoding=[Text.Encoding]::UTF8;[Console]::Out.Write($d.SelectedPath)}')
    env = dict(os.environ, PICK_INIT=initial or '', PICK_TITLE=title)
    r = subprocess.run(['powershell', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-Command', ps],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **NOWIN)
    if r.returncode != 0:
        applog('pickdir ps: ' + r.stderr.decode('utf8', 'ignore')[-300:])
    return r.stdout.decode('utf8', 'ignore').strip() or None


def pick_dir(initial, title='Куди зберегти AAF'):
    if sys.platform == 'darwin':
        try:
            r = subprocess.run(['osascript', '-e', 'POSIX path of (choose folder with prompt "%s")' % title],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return r.stdout.decode('utf8').strip() or None
        except Exception as e:
            applog('pickdir mac: %s' % e)
    for fn in (_pick_tk, _pick_ps) if os.name == 'nt' else (_pick_tk,):
        try:
            return fn(initial, title)
        except Exception as e:
            applog('pickdir %s: %s' % (fn.__name__, e))
    return None


def watchdog():
    t0 = time.time()
    while True:
        time.sleep(5)
        t = STATE['ping'] or (t0 + 75)
        busy = STATE['job'] and not STATE['job'].get('done')
        if t and not busy and time.time() - t > 45:
            os._exit(0)


AUDIO_EXT = ('.wav', '.wave', '.bwf', '.mp3', '.aif', '.aiff', '.aifc', '.m4a', '.flac', '.ogg', '.caf')
LIB = {'dirs': None, 'name': {}, 'stem': {}, 'stamp': 0, 'count': 0}
CFG = os.path.join(HERE, 'config.json')


def load_cfg():
    try:
        with open(CFG, encoding='utf8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(c):
    try:
        with open(CFG, 'w', encoding='utf8') as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
    except Exception as e:
        applog('config: %s' % e)


def nkey(s):
    return unicodedata.normalize('NFC', s).lower()


def index_libs(force=False):
    """індекс усіх аудіофайлів у папках бібліотеки: назва → шлях"""
    dirs = load_cfg().get('libs', [])
    if not force and LIB['dirs'] == dirs and time.time() - LIB['stamp'] < 300:
        return
    name, stem, n = {}, {}, 0
    for d in dirs:
        for root, _, files in os.walk(d):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in AUDIO_EXT:
                    continue
                full = os.path.join(root, fn)
                name.setdefault(nkey(fn), full)
                stem.setdefault(nkey(os.path.splitext(fn)[0]), full)
                n += 1
    LIB.update(dirs=dirs, name=name, stem=stem, stamp=time.time(), count=n)


def make_resolver(pmap):
    """де взяти файл: 1) папка, виправлена вручну; 2) оригінальний шлях; 3) пошук за назвою в бібліотеці"""
    index_libs()

    def resolve(path):
        d = path[:path.rfind('/') + 1]
        to = pmap.get(d, d)
        if to and not to.endswith('/') and not to.endswith('\\'):
            to += '/'
        cand = to + path[len(d):]
        if real_path(cand):
            return cand
        b = path.rsplit('/', 1)[-1]
        hit = LIB['name'].get(nkey(b)) or LIB['stem'].get(nkey(os.path.splitext(b)[0]))
        return hit or cand
    return resolve


def libs_state():
    index_libs()
    return {'dirs': LIB['dirs'] or [], 'count': LIB['count']}


def job_runner(payload):
    st = STATE['job']
    def log(m): st['log'].append(m)
    def progress(m, v): st['msg'] = m; st['p'] = v
    try:
        p = parse_fcpxml(payload['xml'])
        o = payload.get('opts', {})
        clips = filter_clips(p, o.get('mode') == 'nodis', bool(o.get('outside')))
        resolve = make_resolver(payload.get('pathmap', {}))
        outdir = os.path.expanduser(o.get('outdir') or default_outdir())
        os.makedirs(outdir, exist_ok=True)
        fn = re.sub(r'[\\/:*?"<>|]', '_', p['name']) + '.aaf'
        out = os.path.join(outdir, fn)
        r = write_aaf(p, clips, out, o, resolve, log, progress)
        st['result'] = dict(r, path=out)
    except Exception as e:
        st['error'] = str(e)
        log(traceback.format_exc())
    st['done'] = True


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        b = body.encode('utf8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == '/' or self.path.startswith('/?'):
            with open(os.path.join(HERE, 'ui.html'), 'rb') as f:
                return self._send(200, f.read(), 'text/html')
        if self.path in ('/icon.png', '/favicon.ico'):
            with open(os.path.join(HERE, 'icon.png'), 'rb') as f:
                return self._send(200, f.read(), 'image/png')
        if self.path == '/api/info':
            return self._send(200, json.dumps({'app': APP_ID, 'version': UPD.version()}))
        if self.path == '/api/update/check':
            return self._send(200, json.dumps(UPD.check()))
        if self.path == '/api/ping':
            STATE['ping'] = time.time()
            return self._send(200, json.dumps({'outdir': default_outdir()}))
        if self.path == '/api/libs':
            return self._send(200, json.dumps(libs_state()))
        if self.path == '/api/progress':
            j = STATE['job'] or {}
            return self._send(200, json.dumps({k: j.get(k) for k in ('msg', 'p', 'done', 'error', 'result', 'log')}))
        self._send(404, '{}')

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        payload = json.loads(self.rfile.read(n).decode('utf8'))
        try:
            if self.path == '/api/parse':
                p = parse_fcpxml(payload['xml'])
                o = payload.get('opts', {})
                clips = filter_clips(p, o.get('mode') == 'nodis', bool(o.get('outside')))
                tracks = build_tracks(clips, o.get('layout', 'min'), o.get('types'))
                dirs = {}
                for c in p['clips']:
                    d = c['asset']['path'][:c['asset']['path'].rfind('/') + 1]
                    dirs[d] = dirs.get(d, 0) + 1
                resolve = make_resolver(payload.get('pathmap', {}))
                def exists(path):
                    return real_path(resolve(path)) is not None
                files = sorted({c['asset']['path'] for c in clips})
                miss = [f for f in files if not exists(f)]
                rng = None
                if p['vspans']:
                    rng = [fmt_tc(min(s[0] for s in p['vspans']), p['fps']), fmt_tc(max(s[1] for s in p['vspans']), p['fps'])]
                return self._send(200, json.dumps(dict(
                    name=p['name'], fps=p['fps'], dur=fmt_tc(p['seqDur'], p['fps']), total=len(p['clips']),
                    warnings=p['warnings'], dirs=dirs, range=rng, missing=miss, nfiles=len(files), libs=libs_state(),
                    converter='afconvert' if AFCONVERT else ('ffmpeg' if FFMPEG else None),
                    tracks=[dict(name=t['name'], off=t['off'], w=t['w'], clips=[dict(
                        id=u['c']['id'], name=u['c']['name'], tc=fmt_tc(u['c']['time'], p['fps']),
                        dur=round(float(u['c']['duration']), 2), db=u['c']['db'], enabled=u['c']['enabled'],
                        type=u['type'], ch='+'.join(map(str, u['part']))) for u in t['units']]) for t in tracks])))
            if self.path == '/api/convert':
                if STATE['job'] and not STATE['job'].get('done'):
                    return self._send(409, json.dumps({'error': 'Вже конвертую'}))
                STATE['job'] = {'msg': 'Старт…', 'p': 0, 'done': False, 'log': []}
                threading.Thread(target=job_runner, args=(payload,), daemon=True).start()
                return self._send(200, '{"ok":true}')
            if self.path == '/api/update/apply':
                if STATE['job'] and not STATE['job'].get('done'):
                    return self._send(409, json.dumps({'error': 'Зачекай, йде конвертація'}))
                r = UPD.apply()
                self._send(200, json.dumps(r))
                if r.get('ok'):
                    threading.Thread(target=restart, daemon=True).start()
                return
            if self.path == '/api/libs':
                c = load_cfg()
                dirs = c.get('libs', [])
                if payload.get('pick'):
                    d = pick_dir(None, 'Папка з музикою / звуками')
                    if d and d not in dirs:
                        dirs.append(d)
                if payload.get('remove') in dirs:
                    dirs.remove(payload['remove'])
                c['libs'] = dirs
                save_cfg(c)
                index_libs(force=True)
                return self._send(200, json.dumps(libs_state()))
            if self.path == '/api/pickdir':
                return self._send(200, json.dumps({'path': pick_dir(payload.get('initial'))}))
            if self.path == '/api/reveal':
                path = payload.get('path')
                if path and os.path.exists(path):
                    if sys.platform == 'darwin':
                        subprocess.Popen(['open', '-R', path])
                    elif os.name == 'nt':
                        subprocess.Popen(['explorer', '/select,', path])
                return self._send(200, '{}')
            if self.path == '/api/quit':
                self._send(200, '{}')
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
                return
        except Exception as e:
            return self._send(400, json.dumps({'error': str(e)}))
        self._send(404, '{}')


def cli(argv):
    # python3 fcpxml2aaf.py input.fcpxml [out.aaf] [--nodis] [--outside] [--nogain]
    src = argv[0]
    out = next((a for a in argv[1:] if not a.startswith('--')), os.path.splitext(src)[0] + '.aaf')
    with open(src, 'r', encoding='utf8') as f:
        p = parse_fcpxml(f.read())
    clips = filter_clips(p, '--nodis' in argv, '--outside' in argv)
    lay = 'fcp' if '--fcp' in argv else ('tidy' if '--tidy' in argv else 'min')
    r = write_aaf(p, clips, out, {'layout': lay, 'media': 'embed' if '--embed' in argv else 'folder',
                                  'gain': 'auto' if '--gain' in argv else 'none'}, lambda x: x, print,
                  lambda m, v: print('[%3d%%] %s' % (v * 100, m)))
    print(json.dumps(r, ensure_ascii=False))


def restart():
    time.sleep(0.4)
    applog('перезапуск після оновлення')
    kw = {'cwd': HERE, 'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if os.name == 'nt':
        kw['creationflags'] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw['start_new_session'] = True
    subprocess.Popen([sys.executable, os.path.abspath(__file__), '--no-open', '--wait-port'], **kw)
    time.sleep(0.3)
    os._exit(0)


def find_browser():
    cands = []
    if os.name == 'nt':
        pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
        pf = os.environ.get('ProgramFiles', r'C:\Program Files')
        la = os.environ.get('LOCALAPPDATA', '')
        cands = [os.path.join(pf86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
                 os.path.join(pf, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
                 os.path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'),
                 os.path.join(la, 'Google', 'Chrome', 'Application', 'chrome.exe')]
    elif sys.platform == 'darwin':
        cands = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                 '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                 '/Applications/Chromium.app/Contents/MacOS/Chromium']
    else:
        cands = [shutil.which(x) or '' for x in ('google-chrome', 'chromium', 'microsoft-edge')]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def open_window(url):
    exe = find_browser()
    if exe:
        base = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), '.tzv')
        prof = os.path.join(base, 'Tupa_Zelena_Vlada', 'window')
        try:
            subprocess.Popen([exe, '--app=' + url, '--user-data-dir=' + prof, '--window-size=1280,900',
                              '--no-first-run', '--no-default-browser-check'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **({'creationflags': 0x00000008} if os.name == 'nt' else {'start_new_session': True}))
            return
        except Exception as e:
            applog('вікно: ' + str(e))
    webbrowser.open(url)


def already_running(url):
    from urllib.request import urlopen
    try:
        with urlopen(url + 'api/info', timeout=1.5) as r:
            return json.loads(r.read().decode('utf8')).get('app') == APP_ID
    except Exception:
        return False


def main():
    args = sys.argv[1:]
    if args and not args[0].startswith('--'):
        return cli(args)
    url = 'http://127.0.0.1:%d/' % PORT
    srv = None
    deadline = time.time() + (30 if '--wait-port' in args else 0)
    while srv is None:
        try:
            srv = HTTPServer(('127.0.0.1', PORT), H)
        except OSError:
            if '--wait-port' not in args and already_running(url):
                open_window(url)  # друга копія: просто показати вікно
                return
            if time.time() > deadline:
                srv = HTTPServer(('127.0.0.1', 0), H)  # порт зайнятий чимось іншим
                url = 'http://127.0.0.1:%d/' % srv.server_address[1]
            else:
                time.sleep(0.3)
    applog('старт ' + url)
    if '--no-open' not in args:
        threading.Timer(0.3, lambda: open_window(url)).start()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
