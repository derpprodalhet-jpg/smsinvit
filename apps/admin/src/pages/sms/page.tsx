import {
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DollarOutlined,
  KeyOutlined,
  ReloadOutlined,
  WalletOutlined,
} from '@ant-design/icons';
import { Button, Col, Row, Typography } from 'antd';
import { Link } from '../../lib/navigation';
import { useEffect, useState } from 'react';
import { AdminPage } from '../../components/admin-page';
import { AdminErrorAlert } from '../../components/admin-feedback';
import { AdminMetricCard, AdminPageToolbar } from '../../components/admin-metric-card';
import { adminApi } from '../../lib/admin-api';
import { SMS_KEY_STATUS_LABEL, labelOf } from '../../lib/labels';

type Overview = {
  configured?: boolean;
  balanceRaw?: string;
  balanceError?: string;
  keys?: Record<string, number>;
  todayUsed?: number;
  todayFailed?: number;
  todaySupplierCost?: number | string;
};

function asNumber(value: number | string | undefined | null): number {
  if (typeof value === 'number') return value;
  if (value == null || value === '') return 0;
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

export default function SmsOverviewPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setLoadError(null);
    try {
      const { data: row } = await adminApi<Overview>('/api/v1/admin/sms/overview');
      setData(row || null);
    } catch (err) {
      setData(null);
      setLoadError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const keys = data?.keys || {};
  const supplierCost = asNumber(data?.todaySupplierCost);
  const ready = data != null;

  return (
    <AdminPage title="接码概览" description="SMSBower 余额、兑换成本与 CDK 兑换健康度。">
      <AdminPageToolbar
        hint="卡密在「卡密批次」生成并复制，套餐在「套餐管理」配置。"
        actions={
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        }
      />

      {loadError ? (
        <AdminErrorAlert
          message="接码概览加载失败"
          description={loadError}
          onRetry={() => void load()}
          style={{ marginBottom: 16 }}
        />
      ) : null}

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={8}>
          <AdminMetricCard
            tone={!ready ? 'default' : data?.configured ? 'success' : 'error'}
            icon={<ApiOutlined />}
            title="API 配置"
            value={ready ? (data?.configured ? '已配置' : '未配置') : undefined}
            description={<Link href="/sms/settings">接码配置</Link>}
          />
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <AdminMetricCard
            tone={!ready ? 'default' : data?.balanceError ? 'warning' : 'processing'}
            icon={<WalletOutlined />}
            title="供应商余额"
            value={
              ready ? (data?.balanceError ? data.balanceError : data?.balanceRaw || '—') : undefined
            }
          />
        </Col>

        <Col xs={24} sm={12} lg={8}>
          <AdminMetricCard
            tone="default"
            icon={<DollarOutlined />}
            title="成功会话已确认成本"
            value={ready ? `${supplierCost.toFixed(4)} USD` : undefined}
          />
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <AdminMetricCard
            tone="success"
            icon={<CheckCircleOutlined />}
            title="今日成功"
            value={ready ? (data?.todayUsed ?? 0) : undefined}
            description={<Link href="/sms/redeems">CDK 使用记录</Link>}
          />
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <AdminMetricCard
            tone={!ready ? 'default' : (data?.todayFailed ?? 0) > 0 ? 'warning' : 'default'}
            icon={<CloseCircleOutlined />}
            title="今日失败"
            value={ready ? (data?.todayFailed ?? 0) : undefined}
          />
        </Col>
        {(['UNUSED', 'LOCKED', 'USED', 'FAILED', 'EXPIRED', 'DISABLED'] as const).map((status) => (
          <Col key={status} xs={12} sm={8} lg={4}>
            <AdminMetricCard
              icon={<KeyOutlined />}
              title={labelOf(SMS_KEY_STATUS_LABEL, status)}
              value={ready ? (keys[status] ?? 0) : undefined}
            />
          </Col>
        ))}
      </Row>

      <Typography.Paragraph type="secondary" style={{ marginTop: 24 }}>
        查看兑换明细请前往 <Link href="/sms/redeems">CDK 使用记录</Link>。
      </Typography.Paragraph>
    </AdminPage>
  );
}
