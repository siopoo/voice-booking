import assert from "node:assert/strict";

const clientModule = await import("./static/transcription-client.mjs").catch(() => ({}));
const transcribeAudio = clientModule.transcribeAudio || (async () => undefined);
const BackendVoiceRecorder = clientModule.BackendVoiceRecorder || class {
  async start() {}
  stop() {}
};

const calls = [];
const fakeFetch = async (url, options) => {
  calls.push({ url, options });
  return {
    ok: true,
    async json() { return { text: "我想预约基础洗护" }; },
  };
};

const audio = new Blob(["audio-bytes"], { type: "audio/webm" });
const transcript = await transcribeAudio(fakeFetch, audio);

assert.equal(transcript, "我想预约基础洗护");
assert.equal(calls.length, 1);
assert.equal(calls[0].url, "/api/transcribe");
assert.equal(calls[0].options.method, "POST");
assert.equal(calls[0].options.headers["Content-Type"], "audio/webm");
assert.equal(calls[0].options.body, audio);

{
  let recorderInstance;
  const stoppedTracks = [];
  class FakeMediaRecorder {
    constructor() {
      recorderInstance = this;
      this.mimeType = "audio/webm";
    }
    start() {}
    stop() {
      this.ondataavailable({ data: new Blob(["recorded-audio"], { type: "audio/webm" }) });
      this.onstop();
    }
  }
  const states = [];
  const transcriptPromise = new Promise((resolve) => {
    const session = new BackendVoiceRecorder(
      {
        mediaDevices: {
          async getUserMedia() {
            return { getTracks: () => [{ stop: () => stoppedTracks.push("stopped") }] };
          },
        },
        MediaRecorder: FakeMediaRecorder,
        fetchImpl: fakeFetch,
      },
      resolve,
      { onState: (state) => states.push(state) },
    );
    session.start().then(() => {
      assert.equal(session.isRecording, true);
      session.stop();
    });
  });
  assert.equal(await transcriptPromise, "我想预约基础洗护");
  assert.ok(recorderInstance);
  assert.deepEqual(states, ["recording", "transcribing", "idle"]);
  assert.deepEqual(stoppedTracks, ["stopped"]);
}

{
  const constraints = [];
  let virtualTrackStopped = false;
  class FakeMediaRecorder {
    constructor(stream) {
      this.stream = stream;
      this.mimeType = "audio/webm";
    }
    start() {}
    stop() {
      this.ondataavailable({ data: new Blob(["recorded-audio"], { type: "audio/webm" }) });
      this.onstop();
    }
  }
  const physicalStream = {
    getTracks: () => [{ stop() {} }],
    getAudioTracks: () => [{ label: "麦克风阵列 (Intel Smart Sound)" }],
  };
  const mediaDevices = {
    async getUserMedia(requested) {
      constraints.push(requested);
      if (constraints.length === 1) {
        return {
          getTracks: () => [{ stop: () => { virtualTrackStopped = true; } }],
          getAudioTracks: () => [{ label: "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)" }],
        };
      }
      return physicalStream;
    },
    async enumerateDevices() {
      return [
        { kind: "audioinput", deviceId: "virtual", label: "VoiceMeeter Output" },
        { kind: "audioinput", deviceId: "physical", label: "麦克风阵列 (Intel Smart Sound)" },
      ];
    },
  };
  let resolveTranscript;
  const transcriptPromise = new Promise((resolve) => { resolveTranscript = resolve; });
  const session = new BackendVoiceRecorder(
    { mediaDevices, MediaRecorder: FakeMediaRecorder, fetchImpl: fakeFetch },
    resolveTranscript,
  );

  await session.start();
  assert.equal(constraints.length, 2, "虚拟输入应切换到真实麦克风");
  assert.equal(constraints[1].audio.deviceId.exact, "physical");
  assert.equal(virtualTrackStopped, true);
  session.stop();
  assert.equal(await transcriptPromise, "我想预约基础洗护");
}

{
  let silenceCallback;
  const levels = [];
  class FakeMediaRecorder {
    constructor() { this.mimeType = "audio/webm"; }
    start() {}
    stop() {
      this.ondataavailable({ data: new Blob(["recorded-audio"], { type: "audio/webm" }) });
      this.onstop();
    }
  }
  const transcriptPromise = new Promise((resolve) => {
    const session = new BackendVoiceRecorder(
      {
        mediaDevices: {
          async getUserMedia() {
            return { getTracks: () => [{ stop() {} }], getAudioTracks: () => [] };
          },
        },
        MediaRecorder: FakeMediaRecorder,
        fetchImpl: fakeFetch,
        levelMonitorFactory: (_stream, callbacks) => {
          silenceCallback = callbacks.onSilence;
          return { start() { callbacks.onLevel(0.5); }, stop() {} };
        },
      },
      resolve,
      { onLevel: (level) => levels.push(level) },
    );
    session.start().then(() => {
      assert.equal(typeof silenceCallback, "function");
      silenceCallback();
      assert.equal(session.isRecording, false);
    });
  });
  assert.equal(await transcriptPromise, "我想预约基础洗护");
  assert.deepEqual(levels, [0.5]);
}

{
  const requested = [];
  class FakeMediaRecorder {
    constructor() { this.mimeType = "audio/webm"; }
    start() {}
  }
  const mediaDevices = {
    async enumerateDevices() {
      return [
        { kind: "audioinput", deviceId: "default", label: "系统默认" },
        { kind: "audioinput", deviceId: "usb-mic", label: "USB 麦克风" },
        { kind: "audiooutput", deviceId: "speaker", label: "扬声器" },
      ];
    },
    async getUserMedia(constraints) {
      requested.push(constraints);
      return {
        getTracks: () => [{ stop() {} }],
        getAudioTracks: () => [{ label: "USB 麦克风" }],
      };
    },
  };
  const session = new BackendVoiceRecorder(
    { mediaDevices, MediaRecorder: FakeMediaRecorder, fetchImpl: fakeFetch },
    () => {},
  );
  assert.equal(typeof session.listAudioInputs, "function");
  assert.equal(typeof session.setDeviceId, "function");
  assert.deepEqual(await session.listAudioInputs(), [
    { deviceId: "default", label: "系统默认" },
    { deviceId: "usb-mic", label: "USB 麦克风" },
  ]);
  session.setDeviceId("usb-mic");
  await session.start();
  assert.equal(requested[0].audio.deviceId.exact, "usb-mic");
}

console.log("transcription client tests passed");
