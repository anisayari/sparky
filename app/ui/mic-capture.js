// Voice-call input stays in this browser. Send short PCM frames to the local speech gate.
class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Float32Array(2048);
    this.offset = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    let pos = 0;
    while (pos < channel.length) {
      const count = Math.min(channel.length - pos, this.frame.length - this.offset);
      this.frame.set(channel.subarray(pos, pos + count), this.offset);
      pos += count;
      this.offset += count;
      if (this.offset === this.frame.length) {
        this.port.postMessage(this.frame, [this.frame.buffer]);
        this.frame = new Float32Array(2048);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor('sparky-mic', MicCapture);
