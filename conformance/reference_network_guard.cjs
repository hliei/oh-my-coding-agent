const net = require("node:net");
const dgram = require("node:dgram");
const dns = require("node:dns");


const originalConnect = net.Socket.prototype.connect;

function isLoopback(host) {
  if (host === undefined || host === "localhost" || host === "::1") {
    return true;
  }
  if (typeof host !== "string") {
    return false;
  }
  if (host.startsWith("/")) {
    return true;
  }
  const normalized = host.startsWith("[") && host.endsWith("]")
    ? host.slice(1, -1)
    : host;
  return normalized === "0:0:0:0:0:0:0:1" || /^127(?:\.\d{1,3}){3}$/.test(normalized);
}

function connectHost(args) {
  if (typeof args[0] === "object" && args[0] !== null) {
    if (args[0].path !== undefined) {
      return args[0].path;
    }
    return args[0].host;
  }
  if (typeof args[0] === "string") {
    return args[0];
  }
  return typeof args[1] === "string" ? args[1] : undefined;
}

net.Socket.prototype.connect = function guardedConnect(...args) {
  const host = connectHost(args);
  if (!isLoopback(host)) {
    throw new Error(`Reference capture external network denied: ${host}`);
  }
  return originalConnect.apply(this, args);
};

const originalDatagramConnect = dgram.Socket.prototype.connect;
dgram.Socket.prototype.connect = function guardedDatagramConnect(port, address, callback) {
  if (!isLoopback(address)) {
    throw new Error(`Reference capture external UDP denied: ${address}`);
  }
  return originalDatagramConnect.call(this, port, address, callback);
};

const originalDatagramSend = dgram.Socket.prototype.send;
dgram.Socket.prototype.send = function guardedDatagramSend(...args) {
  const portIndex = args.findIndex((value) => typeof value === "number");
  const address = portIndex >= 0 && typeof args[portIndex + 1] === "string"
    ? args[portIndex + 1]
    : undefined;
  if (!isLoopback(address)) {
    throw new Error(`Reference capture external UDP denied: ${address}`);
  }
  return originalDatagramSend.apply(this, args);
};

const originalLookup = dns.lookup;
dns.lookup = function guardedLookup(hostname, ...args) {
  if (!isLoopback(hostname)) {
    throw new Error(`Reference capture external DNS denied: ${hostname}`);
  }
  return originalLookup.call(this, hostname, ...args);
};

for (const name of [
  "resolve",
  "resolve4",
  "resolve6",
  "resolveAny",
  "resolveCaa",
  "resolveCname",
  "resolveMx",
  "resolveNaptr",
  "resolveNs",
  "resolvePtr",
  "resolveSoa",
  "resolveSrv",
  "resolveTxt",
  "reverse",
]) {
  dns[name] = function deniedDnsLookup(hostname) {
    throw new Error(`Reference capture external DNS denied: ${hostname}`);
  };
  dns.promises[name] = async function deniedDnsPromise(hostname) {
    throw new Error(`Reference capture external DNS denied: ${hostname}`);
  };
}
const originalPromiseLookup = dns.promises.lookup;
dns.promises.lookup = async function guardedPromiseLookup(hostname, ...args) {
  if (!isLoopback(hostname)) {
    throw new Error(`Reference capture external DNS denied: ${hostname}`);
  }
  return originalPromiseLookup.call(this, hostname, ...args);
};
