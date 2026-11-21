import { App, ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import type { ReactNode } from 'react';

dayjs.locale('zh-cn');

const adminTheme = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: '#2a7a7b',
    colorInfo: '#2a7a7b',
    colorText: '#17253d',
    colorTextSecondary: '#5c6b7a',
    colorBorderSecondary: '#e4ecee',
    borderRadius: 12,
    colorBgLayout: '#eef3f4',
    controlHeight: 36,
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
  },
  components: {
    Table: {
      headerBorderRadius: 12,
      headerBg: '#f4f8f8',
      headerColor: '#5c6b7a',
      cellPaddingBlock: 14,
    },
    Card: { paddingLG: 20, borderRadiusLG: 16 },
    Layout: {
      headerHeight: 64,
      bodyBg: '#eef3f4',
      siderBg: '#ffffff',
    },
    Menu: {
      itemHeight: 44,
      itemMarginInline: 8,
      itemBorderRadius: 10,
    },
  },
};

export function AntdProvider({ children }: { children: ReactNode }) {
  return (
    <ConfigProvider locale={zhCN} theme={adminTheme}>
      <App>{children}</App>
    </ConfigProvider>
  );
}
