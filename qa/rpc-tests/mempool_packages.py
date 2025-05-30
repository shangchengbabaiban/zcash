#!/usr/bin/env python3
# Copyright (c) 2014-2016 The Bitcoin Core developers
# Copyright (c) 2018-2024 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

# Test descendant package tracking code

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    ROUND_DOWN,
    Decimal,
    JSONRPCException,
    assert_equal,
    connect_nodes,
    start_node,
    sync_blocks,
    sync_mempools,
)
from test_framework.mininode import COIN
from test_framework.zip317 import conventional_fee

def satoshi_round(amount):
    return  Decimal(amount).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)

class MempoolPackagesTest(BitcoinTestFramework):
    limitdescendantcount = 120

    def setup_network(self):
        base_args = [
            '-limitdescendantcount=%d' % (self.limitdescendantcount,),
            '-maxorphantx=%d' % (self.limitdescendantcount,),
            '-debug',
            '-allowdeprecated=getnewaddress',
            '-allowdeprecated=createrawtransaction',
            '-allowdeprecated=signrawtransaction',
        ]
        self.nodes = []
        self.nodes.append(start_node(0, self.options.tmpdir, base_args))
        self.nodes.append(start_node(1, self.options.tmpdir, base_args + ["-limitancestorcount=5"]))
        connect_nodes(self.nodes[0], 1)
        self.is_network_split = False
        self.sync_all()

    # Build a transaction that spends parent_txid:vout
    # Return amount sent
    def chain_transaction(self, node, parent_txid, vout, value, fee, num_outputs):
        send_value = satoshi_round((value - fee)/num_outputs)
        inputs = [ {'txid' : parent_txid, 'vout' : vout} ]
        outputs = {}
        for i in range(num_outputs):
            outputs[node.getnewaddress()] = send_value
        rawtx = node.createrawtransaction(inputs, outputs)
        signedtx = node.signrawtransaction(rawtx)
        txid = node.sendrawtransaction(signedtx['hex'])
        fulltx = node.getrawtransaction(txid, 1)
        assert(len(fulltx['vout']) == num_outputs) # make sure we didn't generate a change output
        return (txid, send_value)

    def run_test(self):
        ''' Mine some blocks and have them mature. '''
        self.nodes[0].generate(101)
        utxo = self.nodes[0].listunspent(10)
        txid = utxo[0]['txid']
        vout = utxo[0]['vout']
        value = utxo[0]['amount']

        fee = conventional_fee(10)
        # 100 transactions off a confirmed tx should be fine
        chain = []
        for i in range(100):
            (txid, sent_value) = self.chain_transaction(self.nodes[0], txid, 0, value, fee, 1)
            value = sent_value
            chain.append(txid)

        # Check mempool has 100 transactions in it, and descendant
        # count and fees should look correct
        mempool = self.nodes[0].getrawmempool(True)
        assert_equal(len(mempool), 100)
        descendant_count = 1
        descendant_fees = 0
        descendant_size = 0

        for x in reversed(chain):
            assert_equal(mempool[x]['descendantcount'], descendant_count)
            descendant_fees += mempool[x]['fee']
            assert_equal(mempool[x]['modifiedfee'], mempool[x]['fee'])
            assert_equal(mempool[x]['descendantfees'], descendant_fees * COIN)
            descendant_size += mempool[x]['size']
            assert_equal(mempool[x]['descendantsize'], descendant_size)
            descendant_count += 1

        # Check that descendant modified fees includes fee deltas from
        # prioritisetransaction
        self.nodes[0].prioritisetransaction(chain[-1], 0, 1000)
        mempool = self.nodes[0].getrawmempool(True)

        descendant_fees = 0
        for x in reversed(chain):
            descendant_fees += mempool[x]['fee']
            assert_equal(mempool[x]['descendantfees'], descendant_fees * COIN + 1000)

        # Adding one more transaction on to the chain should fail.
        try:
            self.chain_transaction(self.nodes[0], txid, vout, value, fee, 1)
        except JSONRPCException:
            print("too-long-ancestor-chain successfully rejected")

        # Check that prioritising a tx before it's added to the mempool works
        [blockhash] = self.nodes[0].generate(1)
        # Ensure that node 1 receives this block before we invalidate it. Otherwise there
        # is a race between node 1 sending a getdata to node 0, and node 0 invalidating
        # the block, that when triggered causes:
        # - node 0 to ignore node 1's "old" getdata;
        # - node 1 to timeout and disconnect node 0;
        # - node 0 and node 1 to have different chain tips, so sync_blocks times out.
        self.sync_all()
        assert_equal(self.nodes[0].getrawmempool(True), {})
        self.nodes[0].prioritisetransaction(chain[-1], None, 2000)
        self.nodes[0].invalidateblock(blockhash)
        mempool = self.nodes[0].getrawmempool(True)

        descendant_fees = 0
        for x in reversed(chain):
            descendant_fees += mempool[x]['fee']
            if (x == chain[-1]):
                assert_equal(mempool[x]['modifiedfee'], mempool[x]['fee']+satoshi_round(0.00002))
            assert_equal(mempool[x]['descendantfees'], descendant_fees * COIN + 2000)

        # TODO: check that node1's mempool is as expected

        # Reconsider the above block to clear the mempool again before the next test phase.
        self.nodes[0].reconsiderblock(blockhash)
        assert_equal(self.nodes[0].getbestblockhash(), blockhash)
        assert_equal(self.nodes[0].getrawmempool(True), {})

        # TODO: test ancestor size limits

        # Now test descendant chain limits
        txid = utxo[1]['txid']
        value = utxo[1]['amount']
        vout = utxo[1]['vout']

        transaction_package = []
        # First create one parent tx with 10 children
        (txid, sent_value) = self.chain_transaction(self.nodes[0], txid, vout, value, fee, 10)
        parent_transaction = txid
        for i in range(10):
            transaction_package.append({'txid': txid, 'vout': i, 'amount': sent_value})

        errored_too_large = False
        for i in range(self.limitdescendantcount):
            utxo = transaction_package.pop(0)
            try:
                (txid, sent_value) = self.chain_transaction(self.nodes[0], utxo['txid'], utxo['vout'], utxo['amount'], fee, 10)
                for j in range(10):
                    transaction_package.append({'txid': txid, 'vout': j, 'amount': sent_value})
                if i == self.limitdescendantcount-2:
                    mempool = self.nodes[0].getrawmempool(True)
                    assert_equal(mempool[parent_transaction]['descendantcount'], self.limitdescendantcount)
            except JSONRPCException as e:
                print(e.error['message'])
                assert_equal(i, self.limitdescendantcount-1)
                print("tx that would create too large descendant package successfully rejected")
                errored_too_large = True
        assert errored_too_large
        assert_equal(len(transaction_package), self.limitdescendantcount * (10 - 1))

        # TODO: check that node1's mempool is as expected

        # TODO: test descendant size limits

        # Test reorg handling
        # First, the basics:
        self.nodes[0].generate(1)
        print("syncing blocks")
        sync_blocks(self.nodes, timeout=480)
        self.nodes[1].invalidateblock(self.nodes[0].getbestblockhash())
        self.nodes[1].reconsiderblock(self.nodes[0].getbestblockhash())

        # Now test the case where node1 has a transaction T in its mempool that
        # depends on transactions A and B which are in a mined block, and the
        # block containing A and B is disconnected, AND B is not accepted back
        # into node1's mempool because its ancestor count is too high.

        # Create 8 transactions, like so:
        # Tx0 -> Tx1 (vout0)
        #   \--> Tx2 (vout1) -> Tx3 -> Tx4 -> Tx5 -> Tx6 -> Tx7
        #
        # Mine them in the next block, then generate a new tx8 that spends
        # Tx1 and Tx7, and add to node1's mempool, then disconnect the
        # last block.

        # Create tx0 with 2 outputs
        utxo = self.nodes[0].listunspent()
        txid = utxo[0]['txid']
        value = utxo[0]['amount']
        vout = utxo[0]['vout']

        fee = conventional_fee(8)
        send_value = satoshi_round((value - fee)/2)
        inputs = [ {'txid' : txid, 'vout' : vout} ]
        outputs = {}
        for i in range(2):
            outputs[self.nodes[0].getnewaddress()] = send_value
        rawtx = self.nodes[0].createrawtransaction(inputs, outputs)
        signedtx = self.nodes[0].signrawtransaction(rawtx)
        txid = self.nodes[0].sendrawtransaction(signedtx['hex'])
        tx0_id = txid
        value = send_value

        # Create tx1
        (tx1_id, tx1_value) = self.chain_transaction(self.nodes[0], tx0_id, 0, value, fee, 1)

        # Create tx2-7
        vout = 1
        txid = tx0_id
        for i in range(6):
            (txid, sent_value) = self.chain_transaction(self.nodes[0], txid, vout, value, fee, 1)
            vout = 0
            value = sent_value

        # Mine these in a block
        self.nodes[0].generate(1)
        self.sync_all()

        # Now generate tx8, with a big fee
        inputs = [ {'txid' : tx1_id, 'vout': 0}, {'txid' : txid, 'vout': 0} ]
        outputs = { self.nodes[0].getnewaddress() : send_value + value - 4*fee }
        rawtx = self.nodes[0].createrawtransaction(inputs, outputs)
        signedtx = self.nodes[0].signrawtransaction(rawtx)
        txid = self.nodes[0].sendrawtransaction(signedtx['hex'])
        sync_mempools(self.nodes)
        
        # Now try to disconnect the tip on each node...
        self.nodes[1].invalidateblock(self.nodes[1].getbestblockhash())
        self.nodes[0].invalidateblock(self.nodes[0].getbestblockhash())
        sync_blocks(self.nodes)

if __name__ == '__main__':
    MempoolPackagesTest().main()
