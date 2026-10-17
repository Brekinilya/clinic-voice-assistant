// Микрофон → PCM 16 бит, 16 кГц, кадры по 512 отсчётов (32 мс) — столько ждёт шлюз.
// Частота AudioContext обычно 48 или 44,1 кГц: перед прореживанием усредняем отсчёты в окне шага,
// чтобы высокие частоты не превратились в шум.

const TARGET_RATE = 16000;
const FRAME = 512;

class PcmWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE;
    this.position = 0; // где в входном потоке следующий выходной отсчёт
    this.pending = new Float32Array(0);
    this.frame = new Int16Array(FRAME);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    const data = new Float32Array(this.pending.length + channel.length);
    data.set(this.pending);
    data.set(channel, this.pending.length);

    const half = this.step / 2;
    while (this.position + half < data.length) {
      const from = Math.max(0, Math.floor(this.position - half));
      const to = Math.min(data.length, Math.ceil(this.position + half));
      let sum = 0;
      for (let i = from; i < to; i++) sum += data[i];
      const value = sum / Math.max(1, to - from);
      this.frame[this.filled++] = Math.max(-32768, Math.min(32767, Math.round(value * 32767)));
      if (this.filled === FRAME) {
        this.port.postMessage(this.frame.buffer.slice(0));
        this.filled = 0;
      }
      this.position += this.step;
    }

    const keep = Math.max(0, Math.floor(this.position - half));
    this.pending = data.slice(keep);
    this.position -= keep;
    return true;
  }
}

registerProcessor("pcm-worklet", PcmWorklet);
