// Browser entry for meshcore-bot's web viewer, bundled with esbuild into
// modules/web_viewer/static/js/meshcore-browser.mjs by build.sh next to this file.
// It re-exports the library and routes Ed25519's SHA-512 through crypto-js,
// so advert signatures verify on a plain-HTTP LAN page where crypto.subtle
// doesn't exist.

import CryptoJS from 'crypto-js';
import * as ed25519 from '@noble/ed25519';
import { MeshCorePacketDecoder } from '../decoder/packet-decoder';
import { DecryptionOptions } from '../types/crypto';
import { hexToBytes, bytesToHex } from '../utils/hex';

export { MeshCorePacketDecoder };
export { MeshCorePacketDecoder as MeshCoreDecoder };
export {
  RouteType,
  PayloadType,
  PayloadVersion,
  DeviceRole,
  AdvertFlags,
  RequestType,
  AnonRequestType,
  TextType,
  ControlSubType
} from '../types/enums';
export { MeshCoreKeyStore } from '../crypto/key-manager';
export { ChannelCrypto } from '../crypto/channel-crypto';
export {
  calcRegionKey,
  calcTransportCode,
  calcTransportCodeForRegion,
  transportCodeMatchesRegion,
  normalizeRegionName,
  REGION_KEY_SIZE
} from '../crypto/region-transport';
export { hexToBytes, bytesToHex, byteToHex, numberToHex } from '../utils/hex';
export {
  getRouteTypeName,
  getPayloadTypeName,
  getPayloadVersionName,
  getDeviceRoleName,
  getRequestTypeName,
  getAnonRequestTypeName,
  getTextTypeName,
  getControlSubTypeName
} from '../utils/enum-names';
export { PathPayloadDecoder } from '../decoder/payload-decoders/path';
export { ed25519 };

function sha512Pure(data: Uint8Array): Uint8Array {
  const wordArray = CryptoJS.lib.WordArray.create(data as unknown as number[]);
  const hash = CryptoJS.SHA512(wordArray);
  const hashBytes = new Uint8Array(64);
  for (let i = 0; i < 16; i++) {
    const word = hash.words[i] || 0;
    hashBytes[i * 4] = (word >>> 24) & 0xff;
    hashBytes[i * 4 + 1] = (word >>> 16) & 0xff;
    hashBytes[i * 4 + 2] = (word >>> 8) & 0xff;
    hashBytes[i * 4 + 3] = word & 0xff;
  }
  return hashBytes;
}

ed25519.etc.sha512Sync = (...messages: Uint8Array[]) => sha512Pure(ed25519.etc.concatBytes(...messages));
ed25519.etc.sha512Async = async (...messages: Uint8Array[]) => sha512Pure(ed25519.etc.concatBytes(...messages));

export class Ed25519SignatureVerifier {
  /**
   * Verify an Ed25519 signature for MeshCore advertisement packets
   * Uses @noble/ed25519 with crypto-js SHA-512 (works in insecure contexts)
   */
  static async verifyAdvertisementSignature(
    publicKeyHex: string,
    signatureHex: string,
    timestamp: number,
    appDataHex: string
  ): Promise<boolean> {
    try {
      const publicKey = hexToBytes(publicKeyHex);
      const signature = hexToBytes(signatureHex);
      const appData = hexToBytes(appDataHex);
      const message = this.constructAdvertSignedMessage(publicKeyHex, timestamp, appData);
      return ed25519.verify(signature, message, publicKey);
    } catch (error) {
      console.error('Ed25519 signature verification failed:', error);
      return false;
    }
  }

  static constructAdvertSignedMessage(publicKeyHex: string, timestamp: number, appData: Uint8Array): Uint8Array {
    const publicKey = hexToBytes(publicKeyHex);
    const timestampBytes = new Uint8Array(4);
    timestampBytes[0] = timestamp & 0xff;
    timestampBytes[1] = (timestamp >> 8) & 0xff;
    timestampBytes[2] = (timestamp >> 16) & 0xff;
    timestampBytes[3] = (timestamp >> 24) & 0xff;
    const message = new Uint8Array(32 + 4 + appData.length);
    message.set(publicKey, 0);
    message.set(timestampBytes, 32);
    message.set(appData, 36);
    return message;
  }

  static getSignedMessageHex(publicKeyHex: string, timestamp: number, appDataHex: string): string {
    const appData = hexToBytes(appDataHex);
    return bytesToHex(this.constructAdvertSignedMessage(publicKeyHex, timestamp, appData));
  }
}

export function decodePacket(hexData: string, options?: DecryptionOptions) {
  return MeshCorePacketDecoder.decode(hexData, options);
}

export function analyzePacket(hexData: string, options?: DecryptionOptions) {
  return MeshCorePacketDecoder.analyzeStructure(hexData, options);
}

export async function decodePacketWithVerification(hexData: string, options?: DecryptionOptions) {
  return MeshCorePacketDecoder.decodeWithVerification(hexData, options);
}

export function isSecureContext(): boolean {
  if (typeof globalThis === 'undefined' || typeof (globalThis as any).window === 'undefined') {
    return true;
  }
  return (globalThis as any).isSecureContext ?? false;
}

export function hasWebCrypto(): boolean {
  return typeof globalThis !== 'undefined' && globalThis.crypto !== undefined && globalThis.crypto.subtle !== undefined;
}
