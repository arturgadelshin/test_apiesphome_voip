import wave

with wave.open('audio.wav', 'rb') as wav_file:
    params = wav_file.getparams()
    print(f"Количество каналов: {params.nchannels}")
    print(f"Ширина сэмпла (байты): {params.sampwidth}")
    print(f"Частота дискретизации (Гц): {params.framerate}")
    print(f"Количество кадров: {params.nframes}")
    print(f"Длительность: {params.nframes / params.framerate:.2f} секунд")