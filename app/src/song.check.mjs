// node --experimental-strip-types src/song.check.mjs   (run by scripts/release.sh)
import assert from "node:assert/strict";
import { songIn } from "./song.ts";

const cases = {
  "make a 15 second reel about concerts with BUNT best day of my life song": "BUNT best day of my life",
  "edit my japan trip to ben bohmer's songs": "ben bohmer",
  "make a video of my trip to japan to Taylor Swift music": "Taylor Swift",
  "cut it to this song https://youtu.be/abc": "https://youtu.be/abc",
  "recap my trip to paris with upbeat music": "",
  "make a reel of the beach day": "",
  "edit it like https://youtu.be/xyz": "",  // a style reference, not a song
};
for (const [p, want] of Object.entries(cases)) assert.equal(songIn(p), want, p);
console.log("ok");
