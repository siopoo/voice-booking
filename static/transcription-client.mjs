import { BrowserAudioLevelMonitor } from "./voice-activity.mjs";

export async function transcribeAudio(fetchImpl, audioBlob) {
  const response = await fetchImpl("/api/transcribe", {
    method: "POST",
    headers: { "Content-Type": audioBlob.type || "application/octet-stream" },
    body: audioBlob,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "语音转文字失败");
  const text = String(data.text || "").trim();
  if (!text) throw new Error("没有识别到语音内容");
  return text;
}

export class BackendVoiceRecorder {
  constructor(dependencies, onTranscript, callbacks = {}) {
    this.mediaDevices = dependencies.mediaDevices;
    this.MediaRecorder = dependencies.MediaRecorder;
    this.fetchImpl = dependencies.fetchImpl;
    this.onTranscript = onTranscript;
    this.onState = callbacks.onState || (() => {});
    this.onError = callbacks.onError || (() => {});
    this.onLevel = callbacks.onLevel || (() => {});
    this.onDevice = callbacks.onDevice || (() => {});
    this.levelMonitorFactory = dependencies.levelMonitorFactory
      || ((stream, monitorCallbacks) => new BrowserAudioLevelMonitor(stream, monitorCallbacks));
    this.isRecording = false;
    this.stream = null;
    this.recorder = null;
    this.chunks = [];
    this.levelMonitor = null;
    this.deviceId = "";
  }

  async start() {
    if (this.isRecording) return;
    try {
      this.stream = await this.openPreferredStream();
      this.onDevice(this.stream.getAudioTracks?.()[0]?.label || "系统默认麦克风");
      this.chunks = [];
      this.recorder = new this.MediaRecorder(this.stream);
      this.recorder.ondataavailable = (event) => {
        if (event.data?.size) this.chunks.push(event.data);
      };
      this.recorder.onstop = () => this.finish();
      this.recorder.start();
      this.isRecording = true;
      this.levelMonitor = this.levelMonitorFactory(this.stream, {
        onLevel: this.onLevel,
        onSilence: () => this.stop(),
      });
      this.levelMonitor.start();
      this.onState("recording");
    } catch (error) {
      this.releaseStream();
      this.onError(error);
      this.onState("idle");
      throw error;
    }
  }

  async openPreferredStream() {
    const processing = {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    };
    if (this.deviceId) {
      return this.mediaDevices.getUserMedia({
        audio: { ...processing, deviceId: { exact: this.deviceId } },
      });
    }
    const initialStream = await this.mediaDevices.getUserMedia({ audio: processing });
    const currentLabel = initialStream.getAudioTracks?.()[0]?.label || "";
    const isVirtual = /voicemeeter|vb-audio|virtual|stereo mix|立体声混音/i.test(currentLabel);
    if (!isVirtual || !this.mediaDevices.enumerateDevices) return initialStream;

    const devices = await this.mediaDevices.enumerateDevices();
    const physicalInput = devices.find((device) => (
      device.kind === "audioinput"
      && device.deviceId
      && device.label
      && !/voicemeeter|vb-audio|virtual|stereo mix|立体声混音/i.test(device.label)
    ));
    if (!physicalInput) return initialStream;

    const physicalStream = await this.mediaDevices.getUserMedia({
      audio: { ...processing, deviceId: { exact: physicalInput.deviceId } },
    });
    initialStream.getTracks().forEach((track) => track.stop());
    return physicalStream;
  }

  async listAudioInputs() {
    if (!this.mediaDevices.enumerateDevices) return [];
    const devices = await this.mediaDevices.enumerateDevices();
    return devices
      .filter((device) => device.kind === "audioinput")
      .map((device, index) => ({
        deviceId: device.deviceId,
        label: device.label || `麦克风 ${index + 1}`,
      }));
  }

  setDeviceId(deviceId) {
    this.deviceId = String(deviceId || "");
  }

  stop() {
    if (!this.isRecording) return;
    this.isRecording = false;
    this.levelMonitor?.stop();
    this.levelMonitor = null;
    this.recorder.stop();
  }

  async finish() {
    const mimeType = this.recorder?.mimeType || this.chunks[0]?.type || "audio/webm";
    const audio = new Blob(this.chunks, { type: mimeType });
    this.releaseStream();
    this.onState("transcribing");
    try {
      const text = await transcribeAudio(this.fetchImpl, audio);
      this.onTranscript(text);
    } catch (error) {
      this.onError(error);
    } finally {
      this.onState("idle");
    }
  }

  releaseStream() {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
  }
}
