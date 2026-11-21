/** Match the existing maintenance API's explicit human review contract. */
export function permitReleaseBody(ownerToken: string, note: string, confirmed: boolean) {
  const explanation = String(note || '').trim();
  if (
    confirmed !== true ||
    explanation.length < 3 ||
    explanation.length > 250 ||
    /[\0\r\n]/.test(explanation)
  ) {
    throw new Error('必须确认所有执行实例已停止，并填写 3 至 250 字的单行复核说明。');
  }
  return { ownerToken, note: explanation, confirmAllExecutorsStopped: true };
}
