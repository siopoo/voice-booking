import assert from "node:assert/strict";

const module = await import("./static/voice-activity.mjs").catch(() => ({}));
const VoiceActivityDetector = module.VoiceActivityDetector || class {
  observe() {}
};

{
  const levels = [];
  let silenceEvents = 0;
  const detector = new VoiceActivityDetector(
    { threshold: 0.08, minSpeechMs: 200, silenceMs: 500 },
    {
      onLevel: (level) => levels.push(level),
      onSilence: () => { silenceEvents += 1; },
    },
  );

  detector.observe(0.01, 0);
  detector.observe(0.12, 100);
  detector.observe(0.15, 350);
  detector.observe(0.02, 600);
  detector.observe(0.01, 1100);
  detector.observe(0.01, 1300);

  assert.equal(silenceEvents, 1, "持续说话后静音 500ms 应自动结束一次录音");
  assert.deepEqual(levels, [0.01, 0.12, 0.15, 0.02, 0.01, 0.01]);
}

{
  let silenceEvents = 0;
  const detector = new VoiceActivityDetector(
    { threshold: 0.08, minSpeechMs: 200, silenceMs: 500 },
    { onSilence: () => { silenceEvents += 1; } },
  );
  detector.observe(0.01, 0);
  detector.observe(0.02, 1000);
  detector.observe(0.01, 3000);
  assert.equal(silenceEvents, 0, "用户尚未开口时不能自动停止");
}

console.log("voice activity tests passed");
