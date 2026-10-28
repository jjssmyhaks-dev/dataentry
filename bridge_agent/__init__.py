"""Bridge agent (Appendix E).

A small program on the customer's PC that connects the cloud engine to TallyPrime.

Design rules it keeps to, all from Appendix E:

* outbound connections only, so no inbound port or firewall change is needed
* one identity per agent, bound to one tenant and one Tally company
* commands are signed by the cloud and verified here, so a stolen connection
  cannot post vouchers
* only structured voucher data arrives; document images never reach the agent
* nothing is lost if the connection or Tally drops: commands and results wait in
  a local queue and go out when it returns
"""

__version__ = "0.1.0"
