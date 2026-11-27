// One release version across all native installers; no network or credentials.
const fs = require('node:fs');
const path = require('node:path');
const root = __dirname;
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const json = name => JSON.parse(read(name));
const pkg = json('package.json');
const config = json('src-tauri/tauri.conf.json');
const lock = json('package-lock.json');
const notes = json('src-tauri/release-notes.json');
const cargo = read('src-tauri/Cargo.toml').split('[package]')[1].split('\n[')[0];
const cargoVersion = cargo.match(/^version\s*=\s*"([^"]+)"/m)?.[1];
const cargoLock = read('src-tauri/Cargo.lock').split('[[package]]').find(block => /^name = "motionfarm-desktop"$/m.test(block));
const lockedVersion = cargoLock?.match(/^version = "([^"]+)"/m)?.[1];
const versions = {package: pkg.version, npmLock: lock.version, npmRoot: lock.packages[''].version,
  tauri: config.version, cargo: cargoVersion, cargoLock: lockedVersion, notes: notes.version};
if (!/^\d+\.\d+\.\d+(?:-beta(?:\.[1-9]\d*)?)?$/.test(pkg.version) || Object.values(versions).some(v => v !== pkg.version)) {
  throw new Error('Release versions differ: ' + JSON.stringify(versions));
}
if (config.productName !== 'kinora' || config.identifier !== 'com.motionfarm.desktop') {
  throw new Error('Keep public name kinora and the existing application identifier');
}
if (config.build.devUrl || config.build.frontendDist !== '../src/renderer') {
  throw new Error('Release must use the bundled frontend');
}
console.log(`kinora ${pkg.version}: all release versions and bundle identity match`);
if (process.argv.includes('--github-output')) {
  if (!process.env.GITHUB_OUTPUT) throw new Error('GITHUB_OUTPUT is required');
  fs.appendFileSync(process.env.GITHUB_OUTPUT, `version=${pkg.version}\n`);
}
