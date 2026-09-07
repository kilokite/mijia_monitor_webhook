(function (root) {
  "use strict";

  const VALID_FOR_MS = 2 * 60 * 60 * 1000;

  function segments(items, field, from, to) {
    if (!(to > from)) return [];
    const devices = new Map();
    for (const item of items) {
      const time = new Date(item.timestamp).getTime();
      if (!Number.isFinite(time) || time >= to || item[field] == null
          || !Number.isFinite(Number(item[field]))) continue;
      const address = item.address || "";
      if (!devices.has(address)) devices.set(address, []);
      devices.get(address).push({ time, value: Number(item[field]), id: Number(item.id) || 0 });
    }
    const result = [];
    for (const [address, readings] of devices) {
      readings.sort((a, b) => a.time - b.time || a.id - b.id);
      readings.forEach((reading, index) => {
        const start = Math.max(from, reading.time);
        const end = Math.min(to, reading.time + VALID_FOR_MS, readings[index + 1]?.time ?? to);
        if (end > start) result.push({ address, start, end, value: reading.value });
      });
    }
    return result;
  }

  function emptyStats() {
    return { weightedSum: 0, duration: 0, minimum: null, maximum: null, average: null };
  }

  function accumulate(stats, value, duration) {
    if (!(duration > 0)) return;
    stats.weightedSum += value * duration;
    stats.duration += duration;
    stats.minimum = stats.minimum == null ? value : Math.min(stats.minimum, value);
    stats.maximum = stats.maximum == null ? value : Math.max(stats.maximum, value);
    stats.average = stats.weightedSum / stats.duration;
  }

  function summarize(intervals) {
    const stats = emptyStats();
    for (const interval of intervals) accumulate(stats, interval.value, interval.end - interval.start);
    return stats;
  }

  function bucketBounds(time, unit) {
    const date = new Date(time);
    if (unit === "day") {
      return [new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime(),
        new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1).getTime()];
    }
    // Preserve the actual hour during a daylight-saving clock rollback.
    const start = time - date.getMinutes() * 60000 - date.getSeconds() * 1000 - date.getMilliseconds();
    return [start, start + 3600000];
  }

  function buckets(intervals, from, to, unit = "hour") {
    const groups = new Map();
    for (let cursor = from; cursor < to;) {
      const [start, end] = bucketBounds(cursor, unit);
      groups.set(start, { ...emptyStats(), start, end, span: Math.min(end, to) - cursor });
      cursor = end;
    }
    for (const interval of intervals) {
      let cursor = Math.max(from, interval.start);
      const limit = Math.min(to, interval.end);
      while (cursor < limit) {
        const [start, end] = bucketBounds(cursor, unit);
        const next = Math.min(end, limit);
        accumulate(groups.get(start), interval.value, next - cursor);
        cursor = next;
      }
    }
    return [...groups.values()];
  }

  function hourlyProfile(groups) {
    const hours = Array.from({ length: 24 }, () => ({ ...emptyStats(), span: 0 }));
    for (const group of groups) {
      const hour = hours[new Date(group.start).getHours()];
      hour.span += group.span;
      if (group.duration > 0) accumulate(hour, group.average, group.duration);
    }
    return hours;
  }

  function coverage(stats, span, devices = 1) {
    return span > 0 && devices > 0 ? Math.min(100, stats.duration / (span * devices) * 100) : 0;
  }

  const api = Object.freeze({ VALID_FOR_MS, segments, summarize, buckets, hourlyProfile, coverage });
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TimeWeighted = api;
})(globalThis);
