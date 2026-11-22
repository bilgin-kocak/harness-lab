OPENQASM 2.0;
include "qelib1.inc";
// A custom gate, a Toffoli inside it, distant two-qubit gates (routing on a line) and a swap;
// two of the four qubits are measured at the end.
gate maj a,b,c { cx c,b; cx c,a; ccx a,b,c; }
qreg q[4];
creg m[2];
h q[0];
rz(0.37) q[1];
u3(0.3,0.2,0.1) q[3];
maj q[0],q[1],q[2];
cx q[0],q[3];
t q[3];
cx q[3],q[1];
swap q[1],q[2];
cz q[2],q[0];
measure q[1] -> m[0];
measure q[3] -> m[1];
