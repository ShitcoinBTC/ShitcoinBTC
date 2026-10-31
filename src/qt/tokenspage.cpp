// Copyright (c) 2026 The Shitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <qt/tokenspage.h>

#include <qt/guiutil.h>
#include <qt/walletmodel.h>

#include <boost/multiprecision/cpp_int.hpp>

#include <QApplication>
#include <QClipboard>
#include <QComboBox>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QInputDialog>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPushButton>
#include <QSettings>
#include <QTableWidget>
#include <QVBoxLayout>

namespace {
using boost::multiprecision::cpp_int;

cpp_int hexToInt(const QString& hex)
{
    cpp_int v = 0;
    QString h = hex.trimmed();
    if (h.startsWith("0x", Qt::CaseInsensitive)) h = h.mid(2);
    for (QChar c : h) {
        unsigned d;
        uint u = c.unicode();
        if (u >= '0' && u <= '9') d = u - '0';
        else if (u >= 'a' && u <= 'f') d = u - 'a' + 10;
        else if (u >= 'A' && u <= 'F') d = u - 'A' + 10;
        else continue;
        v <<= 4;
        v += d;
    }
    return v;
}

QString intToDec(const cpp_int& v)
{
    return QString::fromStdString(v.convert_to<std::string>());
}
} // namespace

TokensPage::TokensPage(QWidget* parent)
    : QWidget(parent)
{
    net = new QNetworkAccessManager(this);

    auto* layout = new QVBoxLayout(this);

    auto* title = new QLabel(tr("NEVM Tokens"), this);
    QFont titleFont = title->font();
    titleFont.setPointSize(titleFont.pointSize() + 4);
    titleFont.setBold(true);
    title->setFont(titleFont);
    layout->addWidget(title);
    layout->addWidget(new QLabel(tr("SHIT-20 tokens and native balance on the Shitcoin NEVM chain (chain ID 57), "
                                    "using the same key as your SHIT wallet."), this));

    // Address / endpoint row
    auto* addrLayout = new QHBoxLayout();
    addrLayout->addWidget(new QLabel(tr("EVM address:"), this));
    addressLabel = new QLabel(tr("(loading)"), this);
    addressLabel->setTextInteractionFlags(Qt::TextSelectableByMouse | Qt::TextSelectableByKeyboard);
    addrLayout->addWidget(addressLabel, 1);
    auto* copyButton = new QPushButton(tr("Copy"), this);
    connect(copyButton, &QPushButton::clicked, this, &TokensPage::onCopyAddressClicked);
    addrLayout->addWidget(copyButton);
    layout->addLayout(addrLayout);

    auto* netLayout = new QHBoxLayout();
    nativeBalanceLabel = new QLabel(tr("Native balance: —"), this);
    netLayout->addWidget(nativeBalanceLabel);
    chainLabel = new QLabel(tr("Chain ID: —"), this);
    netLayout->addWidget(chainLabel);
    netLayout->addStretch(1);
    netLayout->addWidget(new QLabel(tr("NEVM RPC:"), this));
    endpointEdit = new QLineEdit(this);
    endpointEdit->setPlaceholderText("http://127.0.0.1:8545");
    endpointEdit->setMinimumWidth(220);
    netLayout->addWidget(endpointEdit);
    refreshButton = new QPushButton(tr("Refresh"), this);
    connect(refreshButton, &QPushButton::clicked, this, &TokensPage::onRefreshClicked);
    netLayout->addWidget(refreshButton);
    layout->addLayout(netLayout);

    statusLabel = new QLabel(this);
    statusLabel->setWordWrap(true);
    layout->addWidget(statusLabel);

    // Token list
    auto* tokenGroup = new QGroupBox(tr("Tokens"), this);
    auto* tokenLayout = new QVBoxLayout(tokenGroup);
    tokenTable = new QTableWidget(0, 3, tokenGroup);
    tokenTable->setHorizontalHeaderLabels({tr("Token"), tr("Contract"), tr("Balance")});
    tokenTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    tokenTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);
    tokenTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    tokenTable->verticalHeader()->setVisible(false);
    tokenTable->setSelectionBehavior(QAbstractItemView::SelectRows);
    tokenTable->setSelectionMode(QAbstractItemView::SingleSelection);
    tokenTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
    tokenLayout->addWidget(tokenTable);
    auto* tokenButtons = new QHBoxLayout();
    tokenButtons->addStretch(1);
    addTokenButton = new QPushButton(tr("Add token"), tokenGroup);
    connect(addTokenButton, &QPushButton::clicked, this, &TokensPage::onAddTokenClicked);
    tokenButtons->addWidget(addTokenButton);
    removeTokenButton = new QPushButton(tr("Remove"), tokenGroup);
    connect(removeTokenButton, &QPushButton::clicked, this, &TokensPage::onRemoveTokenClicked);
    tokenButtons->addWidget(removeTokenButton);
    tokenLayout->addLayout(tokenButtons);
    layout->addWidget(tokenGroup);

    // Send
    auto* sendGroup = new QGroupBox(tr("Send"), this);
    auto* sendLayout = new QFormLayout(sendGroup);
    sendAssetCombo = new QComboBox(sendGroup);
    sendLayout->addRow(tr("Asset:"), sendAssetCombo);
    sendToEdit = new QLineEdit(sendGroup);
    sendToEdit->setPlaceholderText("0x…");
    sendLayout->addRow(tr("To address:"), sendToEdit);
    sendAmountEdit = new QLineEdit(sendGroup);
    sendAmountEdit->setPlaceholderText("0.0");
    sendLayout->addRow(tr("Amount:"), sendAmountEdit);
    sendInfoLabel = new QLabel(sendGroup);
    sendInfoLabel->setWordWrap(true);
    sendLayout->addRow(sendInfoLabel);
    sendButton = new QPushButton(tr("Send"), sendGroup);
    connect(sendButton, &QPushButton::clicked, this, &TokensPage::onSendClicked);
    sendLayout->addRow(sendButton);
    layout->addWidget(sendGroup);

    layout->addStretch(1);
}

void TokensPage::setWalletModel(WalletModel* walletModel)
{
    this->walletModel = walletModel;
    if (!walletModel) return;
    loadSettings();
    rebuildTokenTable();
    updateEvmAddress();
    refreshAll();
}

void TokensPage::loadSettings()
{
    QSettings s;
    endpointEdit->setText(s.value("TokensPage/endpoint", "http://127.0.0.1:8545").toString());
    tokens.clear();
    int n = s.beginReadArray("TokensPage/tokens");
    for (int i = 0; i < n; ++i) {
        s.setArrayIndex(i);
        TokenInfo ti;
        ti.contract = s.value("contract").toString();
        ti.symbol = s.value("symbol", "TKN").toString();
        ti.decimals = s.value("decimals", 18).toInt();
        if (isValidEvmAddress(ti.contract)) tokens.append(ti);
    }
    s.endArray();
}

void TokensPage::saveSettings()
{
    QSettings s;
    s.setValue("TokensPage/endpoint", endpointEdit->text().trimmed());
    s.beginWriteArray("TokensPage/tokens", tokens.size());
    for (int i = 0; i < tokens.size(); ++i) {
        s.setArrayIndex(i);
        s.setValue("contract", tokens[i].contract);
        s.setValue("symbol", tokens[i].symbol);
        s.setValue("decimals", tokens[i].decimals);
    }
    s.endArray();
}

void TokensPage::rebuildTokenTable()
{
    int selected = sendAssetCombo->currentIndex();
    tokenTable->setRowCount(tokens.size());
    sendAssetCombo->clear();
    sendAssetCombo->addItem(tr("Native (gas token)"));
    for (int i = 0; i < tokens.size(); ++i) {
        const TokenInfo& ti = tokens[i];
        auto* symItem = new QTableWidgetItem(ti.symbol);
        auto* contractItem = new QTableWidgetItem(ti.contract);
        auto* balItem = new QTableWidgetItem(tr("…"));
        tokenTable->setItem(i, 0, symItem);
        tokenTable->setItem(i, 1, contractItem);
        tokenTable->setItem(i, 2, balItem);
        sendAssetCombo->addItem(tr("%1 (%2)").arg(ti.symbol, ti.contract.left(10) + "…"));
    }
    if (selected >= 0 && selected < sendAssetCombo->count()) {
        sendAssetCombo->setCurrentIndex(selected);
    }
}

void TokensPage::updateEvmAddress()
{
    evmAddress.clear();
    if (walletModel) {
        evmAddress = walletModel->getEVMAddress();
    }
    addressLabel->setText(evmAddress.isEmpty() ? tr("(locked — unlock wallet to reveal)") : evmAddress);
}

bool TokensPage::isValidEvmAddress(const QString& addr)
{
    QString h = addr.trimmed();
    if (h.startsWith("0x", Qt::CaseInsensitive)) h = h.mid(2);
    if (h.size() != 40) return false;
    for (QChar c : h) {
        uint u = c.unicode();
        if (!((u >= '0' && u <= '9') || (u >= 'a' && u <= 'f') || (u >= 'A' && u <= 'F'))) return false;
    }
    return true;
}

QString TokensPage::normalizeAddress(const QString& addr)
{
    QString h = addr.trimmed();
    if (h.startsWith("0x", Qt::CaseInsensitive)) h = h.mid(2);
    return "0x" + h.toLower();
}

QString TokensPage::toBaseUnits(const QString& decimal, int decimals, bool& ok)
{
    ok = false;
    QString s = decimal.trimmed();
    if (s.isEmpty()) return {};
    const QStringList parts = s.split('.');
    if (parts.size() > 2) return {};
    for (const QString& p : parts) {
        for (QChar c : p) {
            if (!c.isDigit()) return {};
        }
    }
    QString intPart = parts[0].isEmpty() ? QString("0") : parts[0];
    QString fracPart = parts.size() == 2 ? parts[1] : QString();
    if (fracPart.size() > decimals) return {};
    while (fracPart.size() < decimals) fracPart.append('0');
    QString res = intPart + fracPart;
    int i = 0;
    while (i + 1 < res.size() && res[i] == '0') ++i;
    ok = true;
    return res.mid(i);
}

QString TokensPage::fromBaseUnits(const QString& baseUnits, int decimals)
{
    QString s = baseUnits.trimmed();
    int i = 0;
    while (i + 1 < s.size() && s[i] == '0') ++i;
    s = s.mid(i);
    if (decimals == 0) return s;
    while (s.size() <= decimals) s.prepend('0');
    s.insert(s.size() - decimals, '.');
    while (s.endsWith('0')) s.chop(1);
    if (s.endsWith('.')) s.chop(1);
    return s;
}

QString TokensPage::pad32Hex(const QString& hexNoPrefix)
{
    QString h = hexNoPrefix;
    while (h.size() < 64) h.prepend('0');
    return h;
}

QString TokensPage::encodeAddress32(const QString& address)
{
    QString h = normalizeAddress(address).mid(2);
    return pad32Hex(h);
}

QString TokensPage::encodeUint256(const QString& decimal)
{
    cpp_int v = 0;
    for (QChar c : decimal) {
        v *= 10;
        v += static_cast<unsigned>(c.unicode() - '0');
    }
    unsigned char buf[32] = {0};
    for (int j = 31; j >= 0 && v > 0; --j) {
        buf[j] = static_cast<unsigned char>((v & 0xff).convert_to<unsigned>());
        v >>= 8;
    }
    QString hex;
    hex.reserve(64);
    for (int j = 0; j < 32; ++j) {
        hex += QString("%1").arg(buf[j], 2, 16, QChar('0'));
    }
    return hex;
}

QString TokensPage::decodeAbiString(const QString& hexData, bool& ok)
{
    ok = false;
    QString h = hexData.trimmed();
    if (h.startsWith("0x", Qt::CaseInsensitive)) h = h.mid(2);
    if (h.size() == 64) {
        // bytes32-style symbol (e.g. MKR): trim trailing zero bytes.
        QByteArray b = QByteArray::fromHex(h.toLatin1());
        int n = b.size();
        while (n > 0 && b[n - 1] == '\0') --n;
        if (n <= 0) return {};
        ok = true;
        return QString::fromLatin1(b.left(n));
    }
    if (h.size() < 128) return {};
    bool lenOk = false;
    quint64 len = h.mid(64, 64).toULongLong(&lenOk, 16);
    if (!lenOk || len == 0 || len > 96 || 128 + len * 2 > static_cast<quint64>(h.size())) return {};
    QByteArray b = QByteArray::fromHex(h.mid(128, static_cast<int>(len * 2)).toLatin1());
    ok = true;
    return QString::fromUtf8(b);
}

int TokensPage::decodeAbiUint8(const QString& hexData, bool& ok)
{
    ok = false;
    QString h = hexData.trimmed();
    if (h.startsWith("0x", Qt::CaseInsensitive)) h = h.mid(2);
    if (h.size() < 64) return 0;
    // decimals() returns a 32-byte word; the value is the last byte.
    int v = h.right(2).toInt(&ok, 16);
    return v;
}

void TokensPage::rpcCall(const QString& method, const QJsonArray& params,
                         std::function<void(const QJsonValue& result, const QString& error)> cb)
{
    QJsonObject req;
    req["jsonrpc"] = "2.0";
    req["id"] = ++rpcId;
    req["method"] = method;
    req["params"] = params;
    QNetworkRequest request(QUrl(endpointEdit->text().trimmed()));
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    QNetworkReply* reply = net->post(request, QJsonDocument(req).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [reply, cb, method]() {
        reply->deleteLater();
        if (reply->error() != QNetworkReply::NoError) {
            cb(QJsonValue(), tr("Network error (%1): %2").arg(method, reply->errorString()));
            return;
        }
        QJsonParseError perr;
        QJsonDocument doc = QJsonDocument::fromJson(reply->readAll(), &perr);
        if (perr.error != QJsonParseError::NoError || !doc.isObject()) {
            cb(QJsonValue(), tr("Bad JSON-RPC response for %1").arg(method));
            return;
        }
        QJsonObject obj = doc.object();
        if (obj.contains("error") && obj["error"].isObject()) {
            cb(QJsonValue(), tr("RPC error (%1): %2").arg(method, obj["error"].toObject()["message"].toString()));
            return;
        }
        cb(obj["result"], QString());
    });
}

void TokensPage::setStatus(const QString& msg, bool isError)
{
    statusLabel->setText(msg);
    statusLabel->setStyleSheet(isError ? "color: #ff6b6b;" : "");
}

void TokensPage::setUiEnabled(bool enabled)
{
    refreshButton->setEnabled(enabled);
    addTokenButton->setEnabled(enabled);
    removeTokenButton->setEnabled(enabled);
    sendButton->setEnabled(enabled);
    sendToEdit->setEnabled(enabled);
    sendAmountEdit->setEnabled(enabled);
    sendAssetCombo->setEnabled(enabled);
    endpointEdit->setEnabled(enabled);
}

void TokensPage::onRefreshClicked()
{
    saveSettings();
    refreshAll();
}

void TokensPage::refreshAll()
{
    if (!walletModel || refreshing) return;
    updateEvmAddress();
    if (evmAddress.isEmpty()) {
        setStatus(tr("Wallet is locked or has no keys — unlock to use the Tokens tab."), true);
        return;
    }
    refreshing = true;
    setStatus(tr("Refreshing…"));
    rpcCall("eth_chainId", {}, [this](const QJsonValue& result, const QString& error) {
        if (!error.isEmpty()) {
            setStatus(error, true);
            refreshing = false;
            return;
        }
        QString hex = result.toString();
        if (hex.startsWith("0x", Qt::CaseInsensitive)) hex = hex.mid(2);
        bool ok = false;
        chainId = hex.toULongLong(&ok, 16);
        chainLabel->setText(ok ? tr("Chain ID: %1").arg(chainId) : tr("Chain ID: ?"));
        if (ok && chainId != 57) {
            setStatus(tr("Warning: endpoint reports chain ID %1, expected 57 (Shitcoin NEVM).").arg(chainId), true);
        } else {
            setStatus(tr("Connected."));
        }
        fetchBalances();
        refreshing = false;
    });
}

void TokensPage::fetchBalances()
{
    if (evmAddress.isEmpty()) return;
    rpcCall("eth_getBalance", {evmAddress, "latest"}, [this](const QJsonValue& result, const QString& error) {
        if (!error.isEmpty()) {
            setStatus(error, true);
            return;
        }
        nativeBalanceLabel->setText(tr("Native balance: %1").arg(fromBaseUnits(intToDec(hexToInt(result.toString())), 18)));
    });
    for (const TokenInfo& ti : tokens) {
        const QString contract = ti.contract;
        const int decimals = ti.decimals;
        const QString data = "0x70a08231" + encodeAddress32(evmAddress);
        rpcCall("eth_call", QJsonArray{QJsonObject{{"to", contract}, {"data", data}}, "latest"},
                [this, contract, decimals](const QJsonValue& result, const QString& error) {
                    for (int row = 0; row < tokenTable->rowCount(); ++row) {
                        QTableWidgetItem* c = tokenTable->item(row, 1);
                        if (c && c->text() == contract) {
                            const QString bal = error.isEmpty()
                                ? fromBaseUnits(intToDec(hexToInt(result.toString())), decimals)
                                : tr("error");
                            tokenTable->item(row, 2)->setText(bal);
                            break;
                        }
                    }
                });
    }
}

void TokensPage::onAddTokenClicked()
{
    bool ok = false;
    QString addr = QInputDialog::getText(this, tr("Add SHIT-20 token"),
        tr("Token contract address (0x…):"), QLineEdit::Normal, QString(), &ok);
    if (!ok || addr.trimmed().isEmpty()) return;
    addr = normalizeAddress(addr);
    if (!isValidEvmAddress(addr)) {
        setStatus(tr("That is not a valid EVM address."), true);
        return;
    }
    for (const TokenInfo& ti : tokens) {
        if (ti.contract == addr) {
            setStatus(tr("Token is already in the list."), true);
            return;
        }
    }
    fetchTokenMetadata(addr);
}

void TokensPage::fetchTokenMetadata(const QString& contract)
{
    setStatus(tr("Reading token contract…"));
    setUiEnabled(false);
    rpcCall("eth_call", QJsonArray{QJsonObject{{"to", contract}, {"data", "0x313ce567"}}, "latest"},
            [this, contract](const QJsonValue& result, const QString& error) {
                int decimals = 18;
                if (error.isEmpty()) {
                    bool ok = false;
                    int d = decodeAbiUint8(result.toString(), ok);
                    if (ok) decimals = d;
                }
                rpcCall("eth_call", QJsonArray{QJsonObject{{"to", contract}, {"data", "0x95d89b41"}}, "latest"},
                        [this, contract, decimals](const QJsonValue& result2, const QString& error2) {
                            setUiEnabled(true);
                            QString symbol = tr("TKN");
                            if (error2.isEmpty()) {
                                bool ok = false;
                                QString s = decodeAbiString(result2.toString(), ok);
                                if (ok && !s.isEmpty()) symbol = s;
                            }
                            TokenInfo ti{contract, symbol, decimals};
                            tokens.append(ti);
                            saveSettings();
                            rebuildTokenTable();
                            fetchBalances();
                            setStatus(tr("Added %1 (%2)").arg(symbol, contract));
                        });
            });
}

void TokensPage::onRemoveTokenClicked()
{
    const int row = tokenTable->currentRow();
    if (row < 0 || row >= tokens.size()) return;
    const QString sym = tokens[row].symbol;
    tokens.removeAt(row);
    saveSettings();
    rebuildTokenTable();
    setStatus(tr("Removed %1.").arg(sym));
}

void TokensPage::onCopyAddressClicked()
{
    if (!evmAddress.isEmpty()) {
        QApplication::clipboard()->setText(evmAddress);
        setStatus(tr("EVM address copied."));
    }
}

void TokensPage::onSendClicked()
{
    if (!walletModel) return;
    if (evmAddress.isEmpty()) {
        setStatus(tr("No EVM address — unlock the wallet first."), true);
        return;
    }
    const QString toAddr = sendToEdit->text().trimmed();
    if (!isValidEvmAddress(toAddr)) {
        setStatus(tr("Invalid recipient EVM address."), true);
        return;
    }
    const int idx = sendAssetCombo->currentIndex();
    if (idx < 0 || idx > tokens.size()) return;
    const bool isNative = (idx == 0);
    const TokenInfo ti = isNative ? TokenInfo{} : tokens[idx - 1];
    const int decimals = isNative ? 18 : ti.decimals;

    bool ok = false;
    const QString amountBase = toBaseUnits(sendAmountEdit->text(), decimals, ok);
    if (!ok) {
        setStatus(tr("Invalid amount."), true);
        return;
    }
    if (amountBase == "0") {
        setStatus(tr("Amount must be greater than zero."), true);
        return;
    }

    const QString callTo = isNative ? normalizeAddress(toAddr) : ti.contract;
    const QString valueWei = isNative ? amountBase : QString("0");
    const QString dataHex = isNative ? QString("0x")
                                     : QString("0xa9059cbb") + encodeAddress32(toAddr) + encodeUint256(amountBase);

    if (QMessageBox::question(this, tr("Confirm send"),
            tr("Send %1 %2 to\n%3?").arg(fromBaseUnits(amountBase, decimals),
                                        isNative ? tr("native") : ti.symbol,
                                        normalizeAddress(toAddr))) != QMessageBox::Yes) {
        return;
    }

    setStatus(tr("Preparing transaction…"));
    setUiEnabled(false);

    // 1. nonce
    rpcCall("eth_getTransactionCount", {evmAddress, "latest"},
            [this, callTo, valueWei, dataHex, isNative](const QJsonValue& result, const QString& error) {
                if (!error.isEmpty()) {
                    setStatus(error, true);
                    setUiEnabled(true);
                    return;
                }
                const QString nonce = intToDec(hexToInt(result.toString()));
                // 2. gas price
                rpcCall("eth_gasPrice", {},
                        [this, callTo, valueWei, dataHex, isNative, nonce](const QJsonValue& result2, const QString& error2) {
                            if (!error2.isEmpty()) {
                                setStatus(error2, true);
                                setUiEnabled(true);
                                return;
                            }
                            const QString gasPrice = intToDec(hexToInt(result2.toString()));
                            // 3. gas estimate
                            QJsonObject txObj{{"from", evmAddress}, {"to", callTo}, {"value", QString("0x" + encodeUint256(valueWei))}};
                            if (!isNative) txObj["data"] = dataHex;
                            rpcCall("eth_estimateGas", {txObj},
                                    [this, callTo, valueWei, dataHex, isNative, nonce, gasPrice](const QJsonValue& result3, const QString& error3) {
                                        const QString gasLimit = error3.isEmpty()
                                            ? intToDec(hexToInt(result3.toString()))
                                            : (isNative ? QString("21000") : QString("100000"));
                                        // 4. sign with the wallet's EVM key (may prompt for unlock)
                                        QString signError;
                                        const QString rawHex = walletModel->signEVMTransaction(
                                            callTo, valueWei, dataHex, nonce, gasPrice, gasLimit,
                                            chainId ? chainId : 57, signError);
                                        if (rawHex.isEmpty()) {
                                            setStatus(signError.isEmpty() ? tr("Signing failed.") : signError, true);
                                            setUiEnabled(true);
                                            return;
                                        }
                                        // 5. broadcast
                                        rpcCall("eth_sendRawTransaction", {rawHex},
                                                [this](const QJsonValue& result4, const QString& error4) {
                                                    setUiEnabled(true);
                                                    if (!error4.isEmpty()) {
                                                        setStatus(error4, true);
                                                        return;
                                                    }
                                                    setStatus(tr("Transaction sent: %1").arg(result4.toString()));
                                                    sendToEdit->clear();
                                                    sendAmountEdit->clear();
                                                    fetchBalances();
                                                });
                                    });
                        });
            });
}
