import { defineConfig } from 'astro/config';
import { unified } from '@astrojs/markdown-remark';
import starlight from '@astrojs/starlight';
import sitemap from '@astrojs/sitemap';
import starlightLlmsTxt from 'starlight-llms-txt';
import { sidebar, redirects } from './src/navigation.mjs';
import { sqltideDark, sqltideLight } from './src/code-themes.mjs';
import mintlifyContent from './src/mintlify-content.mjs';

export default defineConfig({
  site: 'https://sqltide.io',
  redirects,
  markdown: { processor: unified({ remarkPlugins: [mintlifyContent] }) },
  integrations: [sitemap({ customPages: ['https://sqltide.io/'] }), starlight({
    title: 'SQLTide',
    description: 'Declarative SQL streaming pipelines for ClickHouse.',
    logo: { light: './src/assets/logo-light.png', dark: './src/assets/logo-dark.png', replacesTitle: true },
    favicon: '/assets/sqltide.png',
    social: [{ icon: 'github', label: 'SQLTide on GitHub', href: 'https://github.com/sqlstack-io/sqltide' }],
    components: {
      PageFrame: './src/components/PageFrame.astro',
      TwoColumnContent: './src/components/TwoColumnContent.astro',
      Header: './src/components/Header.astro',
      PageTitle: './src/components/PageTitle.astro',
      SiteTitle: './src/components/SiteTitle.astro',
      Pagination: './src/components/Pagination.astro',
      Footer: './src/components/Footer.astro',
      ThemeSelect: './src/components/ThemeSelect.astro',
    },
    customCss: ['./src/styles/fonts.css', './src/styles/sqltide.css'],
    expressiveCode: {
      themes: [sqltideDark, sqltideLight],
      plugins: [{
        name: 'keyboard-scrollable-code',
        hooks: { postprocessRenderedBlock: ({ renderData }) => {
          const visit = (node) => {
            if (node.tagName === 'pre') node.properties.tabIndex = 0;
            node.children?.forEach(visit);
          };
          visit(renderData.blockAst);
        } },
      }],
      defaultProps: { overridesByLang: { 'bash,sh,shell,zsh,console': { frame: 'none' } } },
      styleOverrides: {
        borderRadius: '2px', borderWidth: '1px', borderColor: 'var(--sqb-line)',
        codeFontFamily: 'var(--sl-font-mono)', codeFontSize: '13px', codeLineHeight: '1.65',
        uiFontFamily: 'var(--sl-font)',
        frames: { shadowColor: 'transparent', frameBoxShadowCssValue: 'none' },
      },
    },
    sidebar,
    plugins: [starlightLlmsTxt({
      projectName: 'SQLTide',
      description: 'SQLTide compiles typed SQL into ClickHouse streaming pipelines, with explicit plans and retained-history replay.',
    })],
  })],
});
