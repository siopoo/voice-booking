export class VoiceSession {
  constructor(adapter, onTranscript, callbacks = {}) {
    this.adapter = adapter;
    this.onTranscript = onTranscript;
    this.onState = callbacks.onState || (() => {});
    this.notifyError = callbacks.onError || (() => {});
    this.isListening = false;
  }

  start() {
    if (this.isListening) return;
    this.isListening = true;
    this.onState(true);
    this.adapter.start();
  }

  stop() {
    if (!this.isListening) return;
    this.isListening = false;
    this.onState(false);
    this.adapter.stop();
  }

  toggle() {
    if (this.isListening) this.stop();
    else this.start();
  }

  onResult(transcript) {
    const text = String(transcript || "").trim();
    if (!text) return;
    this.stop();
    this.onTranscript(text);
  }

  onError(code) {
    const fatalErrors = new Set(["not-allowed", "service-not-allowed", "audio-capture", "network"]);
    if (fatalErrors.has(code)) {
      this.isListening = false;
      this.onState(false);
    }
    this.notifyError(code);
  }

  onEnd() {
    if (!this.isListening) return;
    try {
      this.adapter.start();
    } catch (error) {
      window.setTimeout(() => {
        if (this.isListening) this.adapter.start();
      }, 180);
    }
  }
}
