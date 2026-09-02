export class VoiceActivityDetector {
  constructor(options = {}, callbacks = {}) {
    this.threshold = options.threshold ?? 0.04;
    this.minSpeechMs = options.minSpeechMs ?? 250;
    this.silenceMs = options.silenceMs ?? 900;
    this.onLevel = callbacks.onLevel || (() => {});
    this.onSilence = callbacks.onSilence || (() => {});
    this.speechStartedAt = null;
    this.speechConfirmed = false;
    this.silenceStartedAt = null;
    this.triggered = false;
  }

  observe(level, now = performance.now()) {
    this.onLevel(level);
    if (this.triggered) return;
    if (level >= this.threshold) {
      if (this.speechStartedAt === null) this.speechStartedAt = now;
      if (now - this.speechStartedAt >= this.minSpeechMs) this.speechConfirmed = true;
      this.silenceStartedAt = null;
      return;
    }
    if (!this.speechConfirmed) return;
    if (this.silenceStartedAt === null) this.silenceStartedAt = now;
    if (now - this.silenceStartedAt >= this.silenceMs) {
      this.triggered = true;
      this.onSilence();
    }
  }
}

export class BrowserAudioLevelMonitor {
  constructor(stream, callbacks = {}, options = {}) {
    this.stream = stream;
    this.callbacks = callbacks;
    this.options = options;
    this.context = null;
    this.source = null;
    this.analyser = null;
    this.frameId = null;
    this.detector = new VoiceActivityDetector(options, callbacks);
  }

  start() {
    if (typeof window === "undefined") return;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    this.context = new AudioContextClass();
    this.source = this.context.createMediaStreamSource(this.stream);
    this.analyser = this.context.createAnalyser();
    this.analyser.fftSize = 512;
    this.source.connect(this.analyser);
    const samples = new Uint8Array(this.analyser.fftSize);
    const tick = (now) => {
      this.analyser.getByteTimeDomainData(samples);
      let energy = 0;
      for (const sample of samples) {
        const normalized = (sample - 128) / 128;
        energy += normalized * normalized;
      }
      this.detector.observe(Math.sqrt(energy / samples.length), now);
      this.frameId = window.requestAnimationFrame(tick);
    };
    this.frameId = window.requestAnimationFrame(tick);
  }

  stop() {
    if (typeof window === "undefined") return;
    if (this.frameId !== null) window.cancelAnimationFrame(this.frameId);
    this.source?.disconnect();
    this.context?.close();
    this.frameId = null;
  }
}
