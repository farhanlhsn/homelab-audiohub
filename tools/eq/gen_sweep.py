import numpy as np, wave, sys
fs = 48000; T = 8.0; f0, f1 = 20.0, 20000.0; amp = 0.07   # about -23 dBFS, sweep is the only loud thing
t = np.arange(int(T*fs))/fs
k = T/np.log(f1/f0)
x = np.sin(2*np.pi*f0*k*(np.exp(t/k)-1))
fade = int(0.05*fs); w = np.ones_like(x); w[:fade] = np.linspace(0,1,fade); w[-fade:] = np.linspace(1,0,fade)
x = (x*w*amp)
pad = np.zeros(int(1.0*fs)); pre = np.random.default_rng(1).standard_normal(int(3.0*fs))*0.0003
sig = np.concatenate([pre, x, pad])
st = np.repeat(sig[:,None], 2, axis=1)
with wave.open(sys.argv[1], 'wb') as f:
    f.setnchannels(2); f.setsampwidth(2); f.setframerate(fs)
    f.writeframes((st*32767).astype('<i2').tobytes())
np.save(sys.argv[1].replace('.wav','.npy'), sig)
print('ok', len(sig)/fs, 's')
