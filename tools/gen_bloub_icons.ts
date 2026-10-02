// 用 bloub（MIT，Jérémy Perret）自己的引擎导出 agent-bridge 会话头像的数据，写 bloub-icons.json。
//   静态：每种身形一条身体路径；每种身形 × 每种表情一对眼睛（眼睛位置会随身形微调，所以按身形分）。
//   动态：引擎 idle 状态 4 秒 40 帧（neutre 表情）—— 身体只取每帧相对第 0 帧的缩放（呼吸），眼睛取每帧的变换矩阵（眨眼 / 漂移）。
// 跑法：把 https://github.com/helefee/bloub 克隆到 tools/bloub，然后在 tools/ 下 npx -y tsx gen_bloub_icons.ts，再把 bloub-icons.json 拷进 agent_bridge/
import { writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { BotEngine } from './bloub/src/bot/engine'
import { SHAPES, COLORS } from './bloub/src/bot/skins'
import { EXPRESSIONS } from './bloub/src/bot/expressions'
import { RAYON, DEMI_VIEWBOX } from './bloub/src/bot/repere'

const here = dirname(fileURLToPath(import.meta.url))
const round = (d: string) => d.replace(/-?\d+\.\d+/g, (m) => String(Math.round(parseFloat(m) * 10) / 10))
const extent = (d: string) => {
  const n = (d.match(/-?\d+(\.\d+)?/g) || []).map(Number)
  return Math.max(...n.map(Math.abs))
}

const out: any = {
  vb: DEMI_VIEWBOX,
  colors: Object.fromEntries(COLORS.map((c) => [c.id, c.hex])),
  exprs: EXPRESSIONS.map((e) => e.id),
  shapes: {},
  eyes: {},
  anim: {}
}
const FRAMES = 40, SECS = 4
const eyeDs = new Set<string>()
for (const s of SHAPES) {
  out.shapes[s.id] = round(new BotEngine(RAYON, 'idle', s.radii, EXPRESSIONS[0]!).sample(0).bodyPath)
  out.eyes[s.id] = {}
  for (const e of EXPRESSIONS) {
    const f = new BotEngine(RAYON, 'idle', s.radii, e).sample(0)
    out.eyes[s.id][e.id] = f.eyes.map((y) => [round(y.d), y.matrix.replace(/^matrix\(|\)$/g, '')])
  }
  const eng = new BotEngine(RAYON, 'idle', s.radii, EXPRESSIONS[0]!)
  const fr = Array.from({ length: FRAMES }, (_, i) => eng.sample((i / FRAMES) * SECS))
  const base = extent(fr[0]!.bodyPath)
  out.anim[s.id] = {
    secs: SECS,
    d: fr[0]!.eyes.map((y) => round(y.d)),
    scale: fr.map((f) => Math.round((extent(f.bodyPath) / base) * 1000) / 1000),
    eyes: fr.map((f) => f.eyes.map((y) => y.matrix.replace(/^matrix\(|\)$/g, '')))
  }
  fr.forEach((f) => f.eyes.forEach((y) => eyeDs.add(round(y.d))))
}
const txt = JSON.stringify(out)
writeFileSync(join(here, 'bloub-icons.json'), txt)
console.log('写出', txt.length, '字节；身形', SHAPES.length, '表情', EXPRESSIONS.length, '颜色', COLORS.length)
console.log('动画帧里眼睛路径的写法共', eyeDs.size, '种（1 种 = 眨眼全靠矩阵，可以只存矩阵）')
console.log('cercle 呼吸缩放范围', Math.min(...out.anim.cercle.scale), '~', Math.max(...out.anim.cercle.scale))
