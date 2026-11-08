import numpy as np, wave, sys, json
from scipy.signal import correlate
fs = 48000
def load(p):
    f = wave.open(p); x = np.frombuffer(f.readframes(f.getnframes()), dtype='<i2')/32768.0; return x
ref = np.load('sweep.npy'); rec = load(sys.argv[1]); noise = load('noise.wav')
# align: find where the sweep starts in the recording (envelope correlation on the sweep part only)
sweep = ref[int(3*fs):int(11*fs)]
c = correlate(rec, sweep, mode='valid', method='fft'); lag = int(np.argmax(np.abs(c)))
start = lag - int(3*fs)                       # recording index of ref[0]
seg = rec[max(start,0): max(start,0)+len(ref)+int(0.6*fs)]   # include tail for the speaker/room decay
N = 1 << int(np.ceil(np.log2(len(seg)+len(ref))))
X = np.fft.rfft(ref, N); Y = np.fft.rfft(seg, N)
f = np.fft.rfftfreq(N, 1/fs)
eps = 1e-3*np.max(np.abs(X))
H = Y*np.conj(X)/(np.abs(X)**2 + eps**2)
mag = np.abs(H)
# 1/6-octave smoothing on a log grid
grid = 20*2**(np.arange(0, np.log2(20000/20)+1e-9, 1/12))
out = []
for g in grid:
    lo, hi = g/2**(1/12), g*2**(1/12)
    m = (f>=lo)&(f<hi)
    out.append(20*np.log10(np.sqrt(np.mean(mag[m]**2))+1e-12))
out = np.array(out)
# SNR estimate per band: sweep-time level vs noise floor in the same bands
Nn = np.fft.rfft(noise[:fs*2], 1<<17); fn = np.fft.rfftfreq(1<<17, 1/fs)
def band_level(spec, ff, g): 
    m=(ff>=g/2**(1/3))&(ff<g*2**(1/3)); return 20*np.log10(np.sqrt(np.mean(np.abs(spec[m])**2))+1e-12)
Ys = np.fft.rfft(seg[:1<<17] if len(seg)>=1<<17 else np.pad(seg,(0,(1<<17)-len(seg))), 1<<17)
print('alignment: sweep starts at %.2fs in recording, peak corr %.3g' % (start/fs, np.max(np.abs(c))))
ref1k = np.interp(np.log2(1000/20)*12, np.arange(len(out)), out)
rel = out - ref1k
np.save('resp_'+sys.argv[2]+'.npy', np.vstack([grid, rel]))
print('rec rms %.1f dBFS, noise rms %.1f dBFS' % (20*np.log10(np.sqrt(np.mean(rec**2))), 20*np.log10(np.sqrt(np.mean(noise**2)))))
print('freq(Hz)  rel to 1kHz (dB)')
for g,r in zip(grid[::2], rel[::2]): print('%7.0f  %+5.1f' % (g, r))
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.figure(figsize=(9,4.2)); plt.semilogx(grid, rel, lw=2); plt.grid(True, which='both', alpha=.3); plt.xlim(20,20000)
plt.xlabel('Hz'); plt.ylabel('dB relatif 1 kHz'); plt.title('Respons Sony ULT Field 1 (mic MacBook, 1/6 oktaf)'); plt.tight_layout(); plt.savefig('resp_'+sys.argv[2]+'.png', dpi=110)
