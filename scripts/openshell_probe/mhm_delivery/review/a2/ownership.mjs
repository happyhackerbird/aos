// Literal predicate specified in D17/D20; this does not assert how omp instantiates extensions.
function instance(){
 let owner;
 return {session_start:id=>{owner??=id;}, allows:id=>id===owner};
}
const shared=instance(); shared.session_start('parent'); shared.session_start('child');
const parent=instance();parent.session_start('parent');
const child=instance();child.session_start('child');
console.log(JSON.stringify({shared_instance_child_allowed:shared.allows('child'),separate_instance_child_allowed:child.allows('child'),runtime_instantiation:'unmeasured'}));
