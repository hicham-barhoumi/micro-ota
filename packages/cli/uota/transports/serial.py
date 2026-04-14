"""
Serial transports for micro-ota host.

RawREPL         — raw REPL file uploader, used by bootstrap only.
SerialOTATransport — OTA over USB/UART0 via raw REPL injection.
                   Enters raw REPL, injects an inline OTA server on the
                   device, then speaks the standard micro-ota protocol
                   directly over the serial port. No extra wiring needed.
"""

import base64
import time
import serial
import serial.tools.list_ports


def auto_detect_port():
    """Return the first serial port that looks like an ESP32."""
    ESP_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}   # CP2102, CH340, FTDI, Espressif native
    for p in serial.tools.list_ports.comports():
        if p.vid in ESP_VIDS:
            return p.device
    # Fallback: first available port
    ports = serial.tools.list_ports.comports()
    if ports:
        return ports[0].device
    return None


class RawREPL:
    """MicroPython raw REPL file uploader."""

    # Max bytes of binary data per exec chunk.
    # base64 overhead is ~4/3, raw REPL exec buffer is generous but
    # keep chunks small for reliability on slow links.
    CHUNK_BINARY = 192

    def __init__(self, port, baud=115200, timeout=5):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None

    def open(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(0.5)
        self._interrupt()
        self._enter_raw()

    def close(self):
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(b'\x02')   # Ctrl+B: exit raw REPL
                self._ser.flush()
            except Exception:
                pass
            self._ser.close()
        self._ser = None

    def soft_reset(self):
        """Exit raw REPL and soft-reset the device."""
        self._ser.write(b'\x02')   # Ctrl+B
        time.sleep(0.1)
        self._ser.write(b'\x04')   # Ctrl+D soft reset
        self._ser.flush()
        time.sleep(1)

    # ── raw REPL protocol ────────────────────────────────────────────────────

    def _interrupt(self):
        self._ser.write(b'\r\x03\x03')
        self._ser.flush()
        time.sleep(0.3)
        self._ser.reset_input_buffer()

    def _enter_raw(self):
        self._ser.write(b'\x01')   # Ctrl+A
        self._ser.flush()
        time.sleep(0.1)
        data = self._ser.read(200)
        if b'raw REPL' not in data:
            # Try once more after another interrupt
            self._interrupt()
            self._ser.write(b'\x01')
            self._ser.flush()
            time.sleep(0.2)
            data = self._ser.read(200)
            if b'raw REPL' not in data:
                raise RuntimeError(
                    'Could not enter raw REPL. Got: ' + repr(data) +
                    '\nCheck the port/baud or press Reset on the device.'
                )

    def exec(self, code):
        """Execute a snippet of Python code. Raises on MicroPython error."""
        if isinstance(code, str):
            code = code.encode()
        self._ser.write(code)
        self._ser.write(b'\x04')   # Ctrl+D: execute
        self._ser.flush()

        # Response: b'OK' + stdout + b'\x04' + stderr + b'\x04'
        header = self._ser.read(2)
        if header != b'OK':
            raise RuntimeError('Raw REPL did not respond OK, got: ' + repr(header))

        out = self._read_until(b'\x04')
        err = self._read_until(b'\x04')
        # Consume the trailing '>' prompt the raw REPL sends after each exec
        self._ser.read(1)
        if err:
            raise RuntimeError('MicroPython: ' + err.decode(errors='replace'))
        return out

    def _read_until(self, sentinel):
        buf = bytearray()
        while True:
            c = self._ser.read(1)
            if not c:
                raise TimeoutError('Timeout reading REPL response')
            if c == sentinel:
                return bytes(buf)
            buf.extend(c)

    # ── filesystem helpers ────────────────────────────────────────────────────

    def makedirs(self, path):
        self.exec(
            "import os\n"
            "_c=''\n"
            "for _p in {!r}.strip('/').split('/'):\n"
            "    _c+='/'+_p\n"
            "    (lambda:None)()\n"
            "    try:os.mkdir(_c)\n"
            "    except:pass\n".format(path)
        )

    def put_file(self, local_path, remote_path, on_progress=None):
        """Upload a local file to the device at remote_path."""
        with open(local_path, 'rb') as f:
            data = f.read()

        total = len(data)
        remote_dir = '/'.join(remote_path.replace('\\', '/').split('/')[:-1])
        if remote_dir:
            self.makedirs(remote_dir)

        # Open file on device
        self.exec("_f=open({!r},'wb')".format(remote_path))

        sent = 0
        while sent < total:
            chunk = data[sent:sent + self.CHUNK_BINARY]
            b64 = base64.b64encode(chunk).decode()
            self.exec(
                "import ubinascii as _u\n"
                "_f.write(_u.a2b_base64({!r}))\n".format(b64)
            )
            sent += len(chunk)
            if on_progress:
                on_progress(sent, total)

        self.exec("_f.close()\ndel _f")
        if on_progress:
            on_progress(total, total)

    def write_text(self, remote_path, content):
        """Write a string directly to a file on the device."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                         delete=False, encoding='utf-8') as tf:
            tf.write(content)
            tmp = tf.name
        try:
            self.put_file(tmp, remote_path)
        finally:
            os.unlink(tmp)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()


# ── SerialOTATransport ────────────────────────────────────────────────────────

# Self-contained inline OTA server injected into the device via raw REPL.
#
# Deliberately does NOT import from the device's /lib/ota.py so it works on:
#   • un-bootstrapped devices (ota.py not yet installed)
#   • devices with an older ota.py that has a different _handle() signature
#
# Uses sys.stdout.buffer / sys.stdin.buffer for raw binary access, bypassing
# the raw-REPL output capture (writes go straight to the UART).
# Falls back to text-mode streams on older MicroPython builds.
_INLINE_SERVER = r"""
import sys,os,json,hashlib,machine,time
try:_O=sys.stdout.buffer;_I=sys.stdin.buffer
except:_O=sys.stdout;_I=sys.stdin
class _C:
 def recv(self,n):
  b=b''
  while len(b)<n:
   c=_I.read(1)
   if not c:raise OSError('eof')
   if isinstance(c,str):c=c.encode('latin-1')
   b+=c
  return b
 def sendall(self,d):
  if isinstance(d,str):d=d.encode('latin-1')
  _O.write(d)
  try:_O.flush()
  except:pass
 def close(self):pass
def _rl(c):
 b=bytearray()
 while True:
  x=c.recv(1)
  if x==b'\n':break
  if x!=b'\r':b.extend(x)
 return bytes(b)
def _re(c,n):
 b=bytearray(n);mv=memoryview(b);p=0
 while p<n:
  x=c.recv(min(512,n-p));mv[p:p+len(x)]=x;p+=len(x)
 return bytes(b)
def _s(c,m):
 if isinstance(m,str):m=m.encode()
 c.sendall(m)
def _isdir(p):
 try:return os.stat(p)[0]&0x4000!=0
 except:return False
def _mkd(p):
 cur=''
 for x in[v for v in p.split('/')if v]:
  cur+='/'+x
  try:os.mkdir(cur)
  except:pass
def _rmt(p):
 try:
  if _isdir(p):
   for e in os.listdir(p):_rmt(p+'/'+e)
   os.rmdir(p)
  else:os.remove(p)
 except:pass
def _hmac(k,m):
 if isinstance(k,str):k=k.encode()
 if isinstance(m,str):m=m.encode()
 B=64
 if len(k)>B:h=hashlib.sha256();h.update(k);k=h.digest()
 k=k+bytes(B-len(k))
 ip=bytes(b^0x36 for b in k);op=bytes(b^0x5C for b in k)
 i=hashlib.sha256();i.update(ip);i.update(m)
 o=hashlib.sha256();o.update(op);o.update(i.digest())
 return ''.join('%02x'%b for b in o.digest())
def _vsig(mf,key):
 if not key:return True
 ls=[mf.get('version','')]
 for p in sorted(mf.get('files',{})):ls.append('{}:{}'.format(p,mf['files'][p]['sha256']))
 return mf.get('sig','')==_hmac(key,'\n'.join(ls))
try:_G=json.load(open('/ota.json'))
except:_G={}
_ST='/ota_stage'
_PR=frozenset(['lib','boot.py','ota.json','ota_manifest.json','ota_version.json','ota_boot_state.json'])
def _ota(c):
 _s(c,'ready\n');mf=None
 try:
  while True:
   h=_rl(c);ps=h.split(b' ',1);cmd=ps[0];arg=ps[1].decode()if len(ps)>1 else''
   if cmd==b'abort':_rmt(_ST);_s(c,'aborted\n');return
   if cmd==b'end_ota':
    if mf:
     try:old=set(json.load(open('/ota_manifest.json')).get('files',{}).keys())
     except:old=set()
     for r in old-set(mf.get('files',{}).keys()):
      try:os.remove('/'+r.lstrip('/'))
      except:pass
     def _wk(d,acc):
      for e in os.listdir(d):
       f=d+'/'+e
       if _isdir(f):_wk(f,acc)
       else:acc.append((f,f[len(_ST):]))
     if _isdir(_ST):
      pairs=[]
      _wk(_ST,pairs)
      for sp,fp in pairs:
       _mkd('/'.join(fp.split('/')[:-1]))
       try:os.remove(fp)
       except:pass
       os.rename(sp,fp)
     _rmt(_ST)
     with open('/ota_manifest.json','w')as f:json.dump(mf,f)
     with open('/ota_version.json','w')as f:json.dump({'version':mf.get('version','unknown')},f)
    _s(c,'ok\n');time.sleep(0.3);machine.reset();return
   if cmd==b'manifest':
    mf=json.loads(_re(c,int(arg)))
    if not _vsig(mf,_G.get('otaKey','')):_rmt(_ST);_s(c,'sig_mismatch\n');return
    _s(c,'ok\n')
   elif cmd==b'file':
    meta=arg.split(';');fn=meta[0];sz=int(meta[1]);ex=meta[2]if len(meta)>2 else None
    sp=_ST+'/'+fn.lstrip('/')
    _mkd('/'.join(sp.split('/')[:-1]))
    h=hashlib.sha256();rem=sz
    with open(sp,'wb')as f:
     while rem>0:
      x=c.recv(min(512,rem));f.write(x);h.update(x);rem-=len(x)
    act=''.join('%02x'%b for b in h.digest())
    if ex and act!=ex:os.remove(sp);_s(c,'sha256_mismatch '+fn+'\n');raise OSError('sha256 mismatch: '+fn)
    _s(c,'ok\n')
   else:_s(c,'unknown\n')
 except Exception as e:
  _rmt(_ST)
  try:_s(c,'error: '+str(e)+'\n')
  except:pass
def _h(c):
 line=_rl(c);ps=line.split(b' ',1);cmd=ps[0];arg=ps[1].decode().strip()if len(ps)>1 else''
 if cmd==b'ping':_s(c,'pong\n')
 elif cmd==b'start_ota':_ota(c)
 elif cmd==b'version':
  try:_s(c,open('/ota_version.json').read()+'\n')
  except:_s(c,'{"version":"unknown"}\n')
 elif cmd==b'ls':
  try:_s(c,'\n'.join(os.listdir(arg or '/'))+'\n')
  except:_s(c,'error\n')
 elif cmd==b'get':
  try:d=open(arg,'rb').read();_s(c,str(len(d))+'\n');_s(c,d)
  except:_s(c,'error\n')
 elif cmd==b'rm':
  try:os.remove(arg);_s(c,'ok\n')
  except:_s(c,'error\n')
 elif cmd==b'reset':
  _s(c,'ok\n');time.sleep(0.3);machine.reset()
 elif cmd==b'wipe':
  for item in os.listdir('/'):
   if item not in _PR:_rmt('/'+item)
  _s(c,'ok\n')
 else:_s(c,'unknown\n')
while True:_h(_C())
"""


class SerialOTATransport:
    """
    OTA transport over USB serial (UART0) using raw REPL injection.

    Usage is identical to WiFiTCPTransport — connect() / read_line() /
    write_line() / read_exact() / write() / close().

    connect() enters raw REPL, executes the inline OTA server on the
    device, and verifies readiness. After that the caller speaks the
    normal micro-ota protocol over the serial port.

    close() sends Ctrl-C + Ctrl-B to restore the interactive REPL.
    """

    def __init__(self, port, baud=115200, timeout=10):
        self.port    = port
        self.baud    = baud
        self.timeout = timeout
        self._ser    = None

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(0.5)

        # Interrupt any running code and flush
        self._ser.write(b'\r\x03\x03')
        self._ser.flush()
        time.sleep(0.3)
        self._ser.reset_input_buffer()

        # Enter raw REPL
        self._ser.write(b'\x01')
        self._ser.flush()
        time.sleep(0.2)
        banner = self._ser.read(200)
        if b'raw REPL' not in banner:
            raise RuntimeError(
                'Could not enter raw REPL. Got: ' + repr(banner) +
                '\nCheck port/baud or press Reset on the device.'
            )

        # Inject inline OTA server
        self._ser.write(_INLINE_SERVER.encode())
        self._ser.write(b'\x04')   # Ctrl+D: execute
        self._ser.flush()

        # Raw REPL replies 'OK' when code starts executing
        header = self._ser.read(2)
        if header != b'OK':
            raise RuntimeError(
                'Inline OTA server failed to start. Got: ' + repr(header)
            )

    def close(self):
        if self._ser and self._ser.is_open:
            try:
                # Ctrl-C interrupts the running loop; Ctrl-B exits raw REPL
                self._ser.write(b'\x03\x02')
                self._ser.flush()
            except Exception:
                pass
            self._ser.close()
        self._ser = None

    # ── protocol primitives ───────────────────────────────────────────────────

    def read_line(self):
        while True:
            buf = bytearray()
            while True:
                c = self._ser.read(1)
                if not c or c == b'\n':
                    break
                if c != b'\r':
                    buf.extend(c)
            line = buf.decode(errors='replace')
            # Skip device debug output (e.g. "[OTA] Manifest: 1 files").
            # sys.stdout cannot be redirected in MicroPython, so debug prints
            # share the UART with protocol responses. All protocol responses
            # are plain words or JSON — they never start with '['.
            if not line.startswith('['):
                return line

    def read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self._ser.read(min(4096, n - len(buf)))
            if not chunk:
                raise OSError('serial connection closed')
            buf.extend(chunk)
        return bytes(buf)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self._ser.write(data)
        self._ser.flush()

    def write_line(self, line):
        self.write(line if line.endswith('\n') else line + '\n')

    def __enter__(self):
        if not (self._ser and self._ser.is_open):
            self.connect()
        return self

    def __exit__(self, *_):
        self.close()
