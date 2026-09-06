import com.android.tools.smali.dexlib2.DexFileFactory;
import com.android.tools.smali.dexlib2.Opcodes;
import com.android.tools.smali.dexlib2.iface.ClassDef;
import com.android.tools.smali.dexlib2.iface.DexFile;
import com.android.tools.smali.dexlib2.writer.pool.DexPool;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Replace one class in a dex while preserving all other class definitions. */
public final class DexReplaceClass {
    private DexReplaceClass() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 4) {
            throw new IllegalArgumentException(
                    "usage: DexReplaceClass <original.dex> <replacement.dex> <descriptor> <output.dex>");
        }

        // telephony-common on this build is dex.039.  Keep that container
        // format; using API 35 here makes DexPool emit dex.041 even though
        // the class uses no newer bytecode features.
        Opcodes loadOpcodes = Opcodes.forApi(35);
        Opcodes opcodes = Opcodes.forApi(28);
        DexFile original = DexFileFactory.loadDexFile(new File(args[0]), loadOpcodes);
        DexFile replacement = DexFileFactory.loadDexFile(new File(args[1]), loadOpcodes);
        String descriptor = args[2];
        ClassDef replacementClass = null;
        for (ClassDef classDef : replacement.getClasses()) {
            if (descriptor.equals(classDef.getType())) {
                replacementClass = classDef;
                break;
            }
        }
        if (replacementClass == null) {
            throw new IllegalArgumentException("replacement class not found: " + descriptor);
        }

        List<ClassDef> classList = new ArrayList<>();
        boolean replaced = false;
        for (ClassDef classDef : original.getClasses()) {
            if (descriptor.equals(classDef.getType())) {
                classList.add(replacementClass);
                replaced = true;
            } else {
                classList.add(classDef);
            }
        }
        if (!replaced) {
            throw new IllegalArgumentException("original class not found: " + descriptor);
        }

        // DexBackedDexFile's class Set does not promise iteration order.  A
        // stable order keeps the merged DEX byte-for-byte reproducible across
        // builds, which is important when selecting the exact artifact to
        // install through a systemless framework overlay.
        classList.sort(Comparator.comparing(ClassDef::getType));
        Set<ClassDef> classes = new LinkedHashSet<>(classList);

        DexFile merged = new DexFile() {
            @Override
            public Set<? extends ClassDef> getClasses() {
                return classes;
            }

            @Override
            public Opcodes getOpcodes() {
                return opcodes;
            }
        };
        DexPool.writeTo(args[3], merged);
    }
}
