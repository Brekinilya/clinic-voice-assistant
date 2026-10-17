// Аудиоклиент голосового шлюза: микрофон → WebSocket, ответы ассистента → динамик.
// Протокол — в app/voice/api.py. Звук ответа приходит бинарным кадром после события speak;
// stop — пациент перебил ассистента: всё, что ещё не прозвучало, выбрасываем.

export class VoiceClient {
  constructor({ url, onEvent = () => {}, onState = () => {} }) {
    this.url = url;
    this.onEvent = onEvent;
    this.onState = onState;
    this.socket = null;
    this.stream = null;
    this.capture = null;
    this.playback = null;
    this.sources = new Set();
    this.nextStart = 0;
    this.pendingRate = null;
  }

  async start() {
    this.onState("connecting");
    // Эхоподавление обязательно: иначе голос ассистента из динамика перебивает его самого
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.capture = new AudioContext();
    await this.capture.audioWorklet.addModule(new URL("./pcm-worklet.js", import.meta.url));
    const source = this.capture.createMediaStreamSource(this.stream);
    const worklet = new AudioWorkletNode(this.capture, "pcm-worklet");
    source.connect(worklet);

    // Ответ играем через <audio>: так браузер учитывает его при эхоподавлении
    this.playback = new AudioContext();
    const output = this.playback.createMediaStreamDestination();
    this.output = output;
    this.audio = new Audio();
    this.audio.srcObject = output.stream;
    await this.audio.play();

    this.socket = new WebSocket(this.url);
    this.socket.binaryType = "arraybuffer";
    this.socket.onopen = () => this.onState("connected");
    this.socket.onclose = () => this.stop();
    this.socket.onmessage = (message) => this.receive(message.data);
    worklet.port.onmessage = (event) => {
      if (this.socket && this.socket.readyState === WebSocket.OPEN) this.socket.send(event.data);
    };
  }

  receive(data) {
    if (data instanceof ArrayBuffer) {
      this.play(data, this.pendingRate || 24000);
      this.pendingRate = null;
      return;
    }
    const event = JSON.parse(data);
    if (event.type === "speak") this.pendingRate = event.sample_rate;
    if (event.type === "stop") this.flush();
    this.onEvent(event);
    if (event.type === "end") this.hangup();
  }

  play(buffer, rate) {
    const pcm = new Int16Array(buffer);
    const samples = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) samples[i] = pcm[i] / 32768;
    const clip = this.playback.createBuffer(1, samples.length, rate);
    clip.copyToChannel(samples, 0);
    const node = this.playback.createBufferSource();
    node.buffer = clip;
    node.connect(this.output);
    const at = Math.max(this.playback.currentTime + 0.05, this.nextStart);
    node.start(at);
    this.nextStart = at + clip.duration;
    this.sources.add(node);
    node.onended = () => this.sources.delete(node);
  }

  flush() {
    for (const node of this.sources) {
      try {
        node.stop();
      } catch (_) {
        // уже доиграл
      }
    }
    this.sources.clear();
    this.nextStart = 0;
  }

  hangup() {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: "hangup" }));
    }
    this.stop();
  }

  stop() {
    if (!this.socket && !this.stream) return;
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    this.stream = null;
    // Доигрываем прощание и только потом закрываем звук
    const tail = Math.max(0, this.nextStart - (this.playback ? this.playback.currentTime : 0));
    const capture = this.capture;
    const playback = this.playback;
    setTimeout(() => {
      if (capture) capture.close();
      if (playback) playback.close();
    }, tail * 1000 + 200);
    this.capture = null;
    this.onState("ended");
  }
}
