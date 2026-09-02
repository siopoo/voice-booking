import assert from "node:assert/strict";
import { VoiceSession } from "./static/voice-session.mjs";


function makeAdapter() {
  return {
    startCount: 0,
    stopCount: 0,
    start() { this.startCount += 1; },
    stop() { this.stopCount += 1; },
  };
}


{
  const adapter = makeAdapter();
  const session = new VoiceSession(adapter, () => {});
  session.start();
  assert.equal(session.isListening, true);
  assert.equal(adapter.startCount, 1);
}

{
  const adapter = makeAdapter();
  const session = new VoiceSession(adapter, () => {});
  session.start();
  session.onEnd();
  assert.equal(session.isListening, true);
  assert.equal(adapter.startCount, 2, "意外结束后应自动重新开始监听");
}

{
  const adapter = makeAdapter();
  const session = new VoiceSession(adapter, () => {});
  session.start();
  session.onError("no-speech");
  session.onEnd();
  assert.equal(session.isListening, true);
  assert.equal(adapter.startCount, 2, "短暂无语音不应结束会话");
}

{
  const adapter = makeAdapter();
  const errors = [];
  const session = new VoiceSession(adapter, () => {}, { onError: (code) => errors.push(code) });
  session.start();
  session.onError("not-allowed");
  assert.equal(session.isListening, false, "权限拒绝属于致命错误，应停止自动重启");
  assert.deepEqual(errors, ["not-allowed"]);
}

{
  const adapter = makeAdapter();
  const errors = [];
  const session = new VoiceSession(adapter, () => {}, { onError: (code) => errors.push(code) });
  session.start();
  session.onError("network");
  session.onEnd();
  assert.equal(session.isListening, false, "网络识别服务不可达时应停止自动重试");
  assert.equal(adapter.startCount, 1, "网络错误后不应反复重启浏览器语音识别");
  assert.deepEqual(errors, ["network"]);
}

{
  const adapter = makeAdapter();
  const transcripts = [];
  const session = new VoiceSession(adapter, (text) => transcripts.push(text));
  session.start();
  session.onResult("我想预约洗护");
  assert.equal(session.isListening, false);
  assert.deepEqual(transcripts, ["我想预约洗护"]);
  assert.equal(adapter.stopCount, 1);
}

console.log("voice session tests passed");
