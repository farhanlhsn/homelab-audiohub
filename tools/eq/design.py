import numpy as np, json
fs = 48000
grid, avg = np.load('avg.npy')
sm = np.convolve(np.pad(avg,(2,1),mode='edge'), np.ones(4)/4, mode='valid')          # ~1/3 octave
def peq(f0, gain, Q):
    A = 10**(gain/40); w = 2*np.pi*f0/fs; al = np.sin(w)/(2*Q)
    b = [1+al*A, -2*np.cos(w), 1-al*A]; a = [1+al/A, -2*np.cos(w), 1-al/A]; return np.array(b)/a[0], np.array(a)/a[0]
def lowshelf(f0, gain, S=0.8):
    A = 10**(gain/40); w = 2*np.pi*f0/fs; al = np.sin(w)/2*np.sqrt((A+1/A)*(1/S-1)+2); c=np.cos(w); sq=2*np.sqrt(A)*al
    b=[A*((A+1)-(A-1)*c+sq), 2*A*((A-1)-(A+1)*c), A*((A+1)-(A-1)*c-sq)]; a=[(A+1)+(A-1)*c+sq, -2*((A-1)+(A+1)*c), (A+1)-(A-1)*c-sq]
    return np.array(b)/a[0], np.array(a)/a[0]
def resp(filters, freqs):
    z = np.exp(-1j*2*np.pi*freqs/fs); h = np.ones_like(freqs, dtype=complex)
    for b,a in filters: h *= (b[0]+b[1]*z+b[2]*z**2)/(a[0]+a[1]*z+a[2]*z**2)
    return 20*np.log10(np.abs(h))
# warm target relative to 1 kHz: +3 dB shelf below ~150 Hz, flat mids, gentle -1 dB/oct above 3 kHz
target = np.where(grid<400, 3.0/(1+(grid/150)**2.5), 0.0) - np.where(grid>3000, np.log2(np.maximum(grid,3000)/3000), 0.0)
ref1k = np.interp(1000, grid, sm); meas = sm - ref1k
want = target - meas
valid = (grid>=80)&(grid<=10000)                      # mic is not trustworthy outside this range
want = np.where(valid, np.clip(want, -5, 4), 0.0)
# fixed, broad bands; gains by regularised least squares so ripple (narrow peaks) is never chased
bands = [(120, 1.0), (250, 0.9), (4000, 0.8), (7000, 1.0)]
basis = np.vstack([resp([peq(f0, 1.0, Q)], grid) for f0, Q in bands]).T      # dB response per +1 dB of gain (near-linear)
w = valid.astype(float)
lam = 0.15
G = np.linalg.solve(basis.T @ (basis * w[:, None]) + lam*np.eye(len(bands)), basis.T @ (want * w))
G = np.clip(np.round(G, 1), -4.0, 4.0)
filters = [peq(f0, g, Q) for (f0, Q), g in zip(bands, G)]
params = [dict(f=float(f0), gain=float(g), Q=float(Q)) for (f0, Q), g in zip(bands, G)]
pre = -round(max(0.0, resp(filters, np.geomspace(20, 20000, 600)).max()) + 0.5, 1)
print('target-vs-measured corrections (dB) after fitting:', ' '.join('%dHz:%+.1f'%(f,g) for f,g in zip(grid[::4].round(), want[::4])))
print('filters:'); [print('  peq %6.0f Hz  %+4.1f dB  Q %.1f' % (p['f'],p['gain'],p['Q'])) for p in params]
print('preamp %.1f dB' % pre)
pred = meas + resp(filters, grid)
json.dump(dict(peq=params, preamp=pre), open('eq.json','w'), indent=1)
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.figure(figsize=(9,4.4))
plt.semilogx(grid, meas, label='terukur (rata-rata 3x)', lw=2); plt.semilogx(grid, pred, label='setelah EQ (prediksi)', lw=2); plt.semilogx(grid, target, '--', label='target hangat', color='gray')
plt.axvspan(20,80,color='k',alpha=.07); plt.axvspan(10000,20000,color='k',alpha=.07)
plt.grid(True, which='both', alpha=.3); plt.xlim(20,20000); plt.ylim(-20,10); plt.legend(); plt.xlabel('Hz'); plt.ylabel('dB relatif 1 kHz'); plt.title('Sony ULT Field 1: koreksi hangat (area abu-abu = di luar jangkauan mic)'); plt.tight_layout(); plt.savefig('design.png', dpi=110)
