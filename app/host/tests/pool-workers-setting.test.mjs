// The CPU cores used for experts is the person's setting (#3417): the field "pool_workers" of Expert settings is saved into the model's run
// config, and --pool-workers goes into its "args" (and "user_args"), so the app's Load, `strata launch`, `strata serve` and Docker read the
// same number. The resource profiles only fill in when the field is blank or 0.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {nativeFixture} from './helpers/native-fixture.mjs';
import {NATIVE_FIELDS, validateNativeSettings} from '../server/native-settings.mjs';
import {resourceSettings} from '../server/native-resources.mjs';

const hw={cpu:{cores:18},ram_gb:128,gpus:[{index:0,name:'NVIDIA GeForce RTX 5070 Ti',vram_gb:16}]};
const workers=(config,profile='work')=>{const args=resourceSettings(config,hw,profile).args,at=args.indexOf('--pool-workers');return at>=0?Number(args[at+1]):null;};

test('the field is a whole number from 0 to 256, and blank or 0 means Automatic',()=>{
  const field=NATIVE_FIELDS.find(f=>f.id==='pool_workers');
  assert.ok(field&&field.type==='number'&&field.min===0,'the field is listed for the Expert settings panel');
  assert.deepEqual(validateNativeSettings({pool_workers:17}),{pool_workers:17});
  assert.deepEqual(validateNativeSettings({pool_workers:0}),{pool_workers:0});
  assert.deepEqual(validateNativeSettings({pool_workers:null}),{pool_workers:null});
  assert.throws(()=>validateNativeSettings({pool_workers:-1}));
  assert.throws(()=>validateNativeSettings({pool_workers:2.5}));
});

test('a saved count goes into args and user_args of the run config, and both profiles keep it',async()=>{
  const f=nativeFixture();try{
    const path=join(f.repo,'strata-iq2_xs.json');await f.control.refresh();
    const loaded=f.control.settings('iq2_xs').values;
    assert.equal(loaded.pool_workers,null,'a file with no setting shows a blank field');
    f.control.save('iq2_xs',{values:{...loaded,pool_workers:17},base:loaded});
    const saved=JSON.parse(readFileSync(path,'utf8'));
    assert.equal(saved.pool_workers,17);
    assert.equal(saved.args[saved.args.indexOf('--pool-workers')+1],'17','the engine reads --pool-workers from every start path');
    assert.deepEqual(saved.user_args,['--pool-workers','17'],'and it is the person\'s option, so the app does not overwrite it');
    assert.equal(workers(saved),17,'18 cores, 17 asked: the Work profile keeps 17 (its rule gives 15)');
    assert.equal(workers(saved,'performance'),17,'and Full performance keeps it too');
  }finally{await f.cleanup();}
});

test('clearing the field removes the flag and the profile rule applies again',async()=>{
  const f=nativeFixture();try{
    const path=join(f.repo,'strata-iq2_xs.json');await f.control.refresh();
    const loaded=f.control.settings('iq2_xs').values;
    f.control.save('iq2_xs',{values:{...loaded,pool_workers:17},base:loaded});
    const after=f.control.settings('iq2_xs').values;
    f.control.save('iq2_xs',{values:{...after,pool_workers:0},base:after});
    const saved=JSON.parse(readFileSync(path,'utf8'));
    assert.equal(saved.args.includes('--pool-workers'),false,'0 takes the flag out, it does not write --pool-workers 0');
    assert.equal(saved.user_args,undefined);
    assert.equal(workers(saved),15,'Automatic again: the measured 80% rule on 18 cores');
  }finally{await f.cleanup();}
});

test('Hy4-Flash and Inkling-2-Mini hide the field: their engines take none of the Flash-Next options',()=>{
  const ui=readFileSync(new URL('../src/components/NativeSettings.tsx',import.meta.url),'utf8').replace(/\r\n/g,'\n');
  assert.match(ui,/HY4_HIDDEN_FIELDS = new Set\(\[[^\]]*'pool_workers'/);
  assert.match(ui,/INKLING_HIDDEN_FIELDS = new Set\(\[[^\]]*'pool_workers'/);
});
