'use strict';
function decodePreview(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 16 || buffer.byteLength > 4000000) throw new Error('预览数据长度错误');
  const bytes = new Uint8Array(buffer);
  const text = new TextDecoder();
  if (text.decode(bytes.subarray(0, 8)) !== 'MFPRV001') throw new Error('不支持的预览版本');
  const headerSize = new DataView(buffer).getUint32(8, true);
  if (headerSize > 65536 || headerSize % 4 || 12 + headerSize > buffer.byteLength) throw new Error('预览头损坏');
  const meta = JSON.parse(text.decode(bytes.subarray(12, 12 + headerSize)));
  const frames = meta.frames, count = meta.joint_count;
  if (!Number.isInteger(frames) || frames < 1 || frames > 3600 || !Number.isInteger(count) || count < 1 || count > 128 || !Number.isFinite(meta.fps) || meta.fps < 1 || meta.fps > 240) throw new Error('预览尺寸错误');
  if (!Array.isArray(meta.parents) || meta.parents.length !== count || !Array.isArray(meta.names) || meta.names.length !== count) throw new Error('预览骨架错误');
  if (buffer.byteLength !== 12 + headerSize + frames * count * 12) throw new Error('预览坐标长度错误');
  const data = new DataView(buffer, 12 + headerSize);
  const flat = new Float32Array(frames * count * 3);
  for (let i = 0; i < flat.length; i++) {
    const value = data.getFloat32(i * 4, true);
    if (!Number.isFinite(value)) throw new Error('预览包含无效坐标');
    flat[i] = value;
  }
  return {meta, buffer:flat.buffer};
}
self.onmessage = event => {
  try { const result = decodePreview(event.data); self.postMessage(result, [result.buffer]); }
  catch (error) { self.postMessage({error:error.message}); }
};
