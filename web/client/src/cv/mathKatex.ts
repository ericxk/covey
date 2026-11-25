/**
 * 公式渲染懒加载的那一块（MdMath.tsx 用 import() 取）：KaTeX、它的样式和字体只在正文里第一次出现公式时才下载，
 * 没有公式的页面不多付这几十 KB。Vite 把这里的 CSS 一起拆进这个 chunk，字体随产物自托管。
 */
import 'katex/dist/katex.min.css';
// 选中公式复制时，剪贴板里是 LaTeX 原文（$…$），不是排版后的碎字
import 'katex/contrib/copy-tex';

export { MathView } from './mathRender';
